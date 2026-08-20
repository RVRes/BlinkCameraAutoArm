import asyncio
import dataclasses
import ipaddress
import logging
import time

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    filters,
)

from blink_service import BlinkService
from config import AppConfig, Config
from presence_monitor import PresenceMonitor
from state import AppState

_LOGGER = logging.getLogger(__name__)

_HELP_TEXT = (
    "Commands (all under /cambot):\n"
    "help\n"
    "status\n"
    "enable | disable\n"
    "arm [name]\n"
    "disarm [name]\n"
    "cameras list\n"
    "cameras add <name>\n"
    "cameras remove <name>\n"
    "cameras refresh\n"
    "ips list\n"
    "ips add <ip>\n"
    "ips remove <ip>\n"
    "2fa <code>\n"
    "snapshot <name>\n"
    "clip <name>\n"
    "alerts on | off\n"
    "settings show\n"
    "settings ping_interval <seconds>\n"
    "settings absence_checks <count>"
)


class TelegramBot:
    """Telegram bot with authorization gate and noun-verb command routing."""

    BOT_INVOCATION_COMMAND = "cambot"

    def __init__(
        self,
        token: str,
        chat_id: int,
        allowed_user_id: int,
        config: Config,
        app_cfg: AppConfig,
        state: AppState,
        blink: BlinkService,
        monitor: PresenceMonitor,
    ):
        """Store dependencies for command handling and outbound messages."""
        self.token = token
        self.chat_id = chat_id
        self.allowed_user_id = allowed_user_id
        self.config = config
        self.app_cfg = app_cfg
        self.state = state
        self.blink = blink
        self.monitor = monitor
        self._application: Application | None = None
        # start() must stay alive for the app's lifetime — like
        # run_main_loop(), it should only end via cancellation, not
        # merely because polling has finished being *set up*. PTB's
        # start_polling() itself only blocks until the background
        # polling task is confirmed running, then returns immediately;
        # without this event, start() (and thus its asyncio task) would
        # complete right after that, which main()'s asyncio.wait(...,
        # return_when=FIRST_COMPLETED) would misread as "the bot task is
        # done" and tear down the whole app within about a second of
        # every launch. shutdown() sets this event to let start() return.
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """Build Application, register handlers, start polling, then
        block until shutdown() is called (or this task is cancelled) —
        so the task stays alive for the app's lifetime, not just for the
        duration of setting polling up."""
        application = Application.builder().token(self.token).build()
        application.add_handler(
            CommandHandler(self.BOT_INVOCATION_COMMAND, self.handle_command)
        )
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message)
        )
        application.add_error_handler(self._on_error)
        self._application = application

        await application.initialize()
        await application.start()
        assert application.updater is not None
        await application.updater.start_polling()
        _LOGGER.info("Telegram bot started and polling for updates.")
        await self.send_message("Bot started.")
        await self._stop_event.wait()

    async def shutdown(self) -> None:
        """Stop polling and cleanly release Application/HTTP resources.

        Safe to call even if start() was never called or failed partway
        through (idempotent no-op in that case).

        Also sets the stop event so a still-running start() task returns
        on its own rather than relying solely on external cancellation —
        harmless if start()'s task has already been cancelled by the
        caller (main()'s shutdown sequence does both).
        """
        self._stop_event.set()
        application = self._application
        if application is None:
            return
        try:
            if application.updater is not None:
                await application.updater.stop()
            await application.stop()
            await application.shutdown()
            _LOGGER.info("Telegram bot shut down cleanly.")
        except Exception:
            _LOGGER.exception("Error while shutting down Telegram bot.")

    async def _on_error(self, update: object, context: CallbackContext) -> None:
        """PTB error handler — logs unhandled exceptions from handlers
        instead of relying on library defaults, and best-effort informs
        the user their command failed."""
        _LOGGER.error(
            "Unhandled exception while processing update %s",
            update,
            exc_info=context.error,
        )
        if isinstance(update, Update) and self._is_authorized(update):
            try:
                await context.bot.send_message(
                    chat_id=self.chat_id,
                    text="An internal error occurred handling that command.",
                )
            except TelegramError:
                _LOGGER.exception("Failed to notify user about handler error.")

    # --- Authorization ---

    def _is_authorized(self, update: Update) -> bool:
        """Check the update's chat and user against the allowlist."""
        if update.effective_chat is None or update.effective_user is None:
            return False
        chat_ok = update.effective_chat.id == self.chat_id
        user_ok = update.effective_user.id == self.allowed_user_id
        if not (chat_ok and user_ok):
            _LOGGER.debug(
                "Unauthorized access attempt from chat=%s user=%s",
                getattr(update.effective_chat, "id", None),
                getattr(update.effective_user, "id", None),
            )
            return False
        return True

    # --- Command dispatch ---

    async def handle_command(
        self, update: Update, context: CallbackContext
    ) -> None:
        """Single CommandHandler for 'cambot'. Routes based on args."""
        if not self._is_authorized(update):
            return

        args = context.args or []
        if not args:
            await self._reply(context, _HELP_TEXT)
            return

        noun = args[0].lower()
        rest = args[1:]

        handlers = {
            "help": lambda: self._cmd_help(context),
            "status": lambda: self._cmd_status(context),
            "enable": lambda: self._cmd_enable(context),
            "disable": lambda: self._cmd_disable(context),
            "arm": lambda: self._cmd_arm(context, rest),
            "disarm": lambda: self._cmd_disarm(context, rest),
            "cameras": lambda: self._cmd_cameras(context, rest),
            "ips": lambda: self._cmd_ips(context, rest),
            "2fa": lambda: self._cmd_2fa(context, rest),
            "snapshot": lambda: self._cmd_snapshot(context, rest),
            "clip": lambda: self._cmd_clip(context, rest),
            "alerts": lambda: self._cmd_alerts(context, rest),
            "settings": lambda: self._cmd_settings(context, rest),
        }

        handler = handlers.get(noun)
        if handler is None:
            await self._reply(
                context, f"Unknown command '{noun}'.\n\n{_HELP_TEXT}"
            )
            return

        await handler()

    async def handle_message(
        self, update: Update, context: CallbackContext
    ) -> None:
        """Plain-text message handler — auth-gated, no-op otherwise."""
        if not self._is_authorized(update):
            return

    # --- Transactional config persistence ---

    def _persist_config_change(
        self, **field_updates: object
    ) -> AppConfig | None:
        """Build a candidate AppConfig with `field_updates` applied,
        validate + persist it via Config.save(), and only on success
        apply the same changes to the live `self.app_cfg`.

        Returns the updated (and now-live) AppConfig on success, or None
        if validation/persistence failed — in which case `self.app_cfg`
        is left completely untouched, so in-memory state can never drift
        from what's on disk. Callers are responsible for awaiting a
        reply in the None case using the raised error's message. Takes
        no CallbackContext — callers that have one (Telegram command
        handlers) reply separately; this also lets non-command callers
        (e.g. the main loop's stale-camera reconciliation) reuse it.
        """
        candidate = dataclasses.replace(self.app_cfg, **field_updates)
        try:
            self.config.save(candidate)
        except (ValueError, OSError):
            _LOGGER.exception(
                "Failed to persist config change: %s", field_updates
            )
            return None
        for name, value in field_updates.items():
            setattr(self.app_cfg, name, value)
        return self.app_cfg

    # --- Sub-handlers ---

    async def _cmd_help(self, context: CallbackContext) -> None:
        """Reply with the full command reference."""
        await self._reply(context, _HELP_TEXT)

    async def _cmd_status(self, context: CallbackContext) -> None:
        """Reply with app state, camera status, and IP presence."""
        cfg = self.app_cfg
        state = self.state

        lines = [
            f"App: {'enabled' if state.is_app_enabled else 'disabled'}",
            (f"Motion alerts: {'on' if cfg.motion_alerts_enabled else 'off'}"),
            (
                "Camera auto-arm: "
                f"{', '.join(cfg.controlled_cameras) or 'none configured'}"
            ),
            "",
        ]

        if state.is_2fa_pending:
            lines.append("Blink API: 2FA pending — send /cambot 2fa <code>")
        elif not self.blink.is_connected:
            lines.append("Blink API: not connected")
        else:
            lines.append("Cameras:")
            for cam in self.blink.list_all_cameras():
                controlled = cam.name in cfg.controlled_cameras
                lines.append("")
                lines.append(f"  {cam.name}")
                lines.append(f"    armed: {'yes' if cam.armed else 'no'}")
                lines.append(f"    online: {'yes' if cam.online else 'no'}")
                lines.append(f"    battery: {cam.battery}")
                lines.append(f"    controlled: {'yes' if controlled else 'no'}")

        lines.append("")
        lines.append("IPs:")
        for ip in cfg.monitored_ips:
            online = state.ip_ping_status.get(ip)
            status = (
                "online"
                if online
                else "offline" if online is False else "unknown"
            )
            lines.append(f"  {ip} — {status}")

        lines.append("")
        lines.append(f"Last arm/disarm: {self._last_arm_change_text()}")
        lines.append(
            "Last main-loop iteration: "
            f"{self._elapsed_text(state.time_of_last_iteration)}"
        )

        await self._reply(context, "\n".join(lines))

    def _last_arm_change_text(self) -> str:
        """Format time since last arm/disarm as 'Xd XXh XXm ago' or 'Never'."""
        return self._elapsed_text(self.state.time_of_last_arm_change)

    @staticmethod
    def _elapsed_text(timestamp: float | None) -> str:
        """Format time elapsed since `timestamp` as 'Xd XXh XXm ago', or
        'Never' if `timestamp` is None — shared by the arm/disarm and
        main-loop-iteration status lines."""
        if not timestamp:
            return "Never"
        elapsed = time.time() - timestamp
        days, remainder = divmod(elapsed, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{int(days)}d {int(hours):02d}h {int(minutes):02d}m ago"

    async def _cmd_enable(self, context: CallbackContext) -> None:
        """Enable automatic arming."""
        self.state.is_app_enabled = True
        _LOGGER.info("Auto-arming enabled via Telegram command.")
        await self._reply(context, "Auto-arming application enabled.")

    async def _cmd_disable(self, context: CallbackContext) -> None:
        """Disable automatic arming."""
        self.state.is_app_enabled = False
        _LOGGER.info("Auto-arming disabled via Telegram command.")
        await self._reply(context, "Auto-arming application disabled.")

    async def _cmd_arm(self, context: CallbackContext, args: list[str]) -> None:
        """Manually arm one or all controlled cameras, independent of
        is_app_enabled — bypasses the main loop entirely."""
        await self._manual_arm_disarm(context, args, armed=True)

    async def _cmd_disarm(
        self, context: CallbackContext, args: list[str]
    ) -> None:
        """Manually disarm one or all controlled cameras, independent of
        is_app_enabled — bypasses the main loop entirely."""
        await self._manual_arm_disarm(context, args, armed=False)

    async def _manual_arm_disarm(
        self, context: CallbackContext, args: list[str], *, armed: bool
    ) -> None:
        """Shared implementation for _cmd_arm/_cmd_disarm.

        With a camera name argument, targets only that camera (must be
        in controlled_cameras). With no argument, targets every camera
        currently in controlled_cameras. Mirrors run_iteration()'s
        auto-arm/disarm branch: calls BlinkService directly and updates
        state.commanded_camera_states + state.time_of_last_arm_change,
        so a manual arm/disarm is indistinguishable from an automatic
        one from the perspective of "what does the API say is armed
        right now" — and is therefore left alone by the main loop on its
        next tick unless presence flips.
        """
        verb = "arm" if armed else "disarm"
        name = " ".join(args).strip()

        if name:
            if name not in self.app_cfg.controlled_cameras:
                await self._reply(
                    context,
                    f"Camera '{name}' is not in the auto-arm list.",
                )
                return
            names = [name]
        else:
            names = list(self.app_cfg.controlled_cameras)
            if not names:
                await self._reply(context, "No controlled cameras configured.")
                return

        if not self.blink.is_connected:
            await self._reply(context, "Not connected to Blink API.")
            return

        if armed:
            results = await self.blink.arm_cameras(names)
        else:
            results = await self.blink.disarm_cameras(names)

        succeeded = [n for n in names if results.get(n)]
        failed = [n for n in names if not results.get(n)]

        if succeeded:
            now = time.time()
            for n in succeeded:
                self.state.commanded_camera_states[n] = armed
            self.state.time_of_last_arm_change = now
            _LOGGER.info(
                "Manually %sed camera(s) via Telegram command: %s",
                verb,
                ", ".join(succeeded),
            )

        lines = []
        if succeeded:
            lines.append(f"{verb.capitalize()}ed: {', '.join(succeeded)}.")
        if failed:
            lines.append(
                f"Not found in Blink account, skipped: {', '.join(failed)}."
            )
        await self._reply(context, "\n".join(lines))

    async def _cmd_cameras(
        self, context: CallbackContext, args: list[str]
    ) -> None:
        """Route 'cameras' noun to its list/add/remove/refresh verb
        handler."""
        if not args:
            await self._reply(
                context,
                f"Usage: cameras list|add|remove|refresh\n\n{_HELP_TEXT}",
            )
            return

        verb = args[0].lower()
        name = " ".join(args[1:]).strip()

        if verb == "list":
            await self._cameras_list(context)
        elif verb == "add":
            await self._cameras_add(context, name)
        elif verb == "remove":
            await self._cameras_remove(context, name)
        elif verb == "refresh":
            await self._cameras_refresh(context)
        else:
            await self._reply(
                context, f"Unknown 'cameras' subcommand '{verb}'."
            )

    async def _cameras_list(self, context: CallbackContext) -> None:
        """Reply with every Blink account camera, marking controlled ones."""
        if not self.blink.is_connected:
            await self._reply(
                context,
                "Not connected to Blink API — cannot list account cameras.",
            )
            return

        cameras = self.blink.list_all_cameras()
        lines = ["Cameras:"]
        for cam in cameras:
            controlled = cam.name in self.app_cfg.controlled_cameras
            marker = " [controlled]" if controlled else ""
            lines.append(f"  {cam.name}{marker}")
        await self._reply(context, "\n".join(lines))

    async def _cameras_add(self, context: CallbackContext, name: str) -> None:
        """Add a validated camera name to the controlled set."""
        if not name:
            await self._reply(
                context, f"Usage: cameras add <name>\n\n{_HELP_TEXT}"
            )
            return

        if not self.blink.is_connected:
            await self._reply(
                context,
                "Not connected to Blink API — cannot validate camera "
                "name. Try again after connection is established.",
            )
            return

        known_names = {cam.name for cam in self.blink.list_all_cameras()}
        if name not in known_names:
            await self._reply(
                context, f"Error: camera '{name}' not found in Blink account."
            )
            return

        if name not in self.app_cfg.controlled_cameras:
            updated = self._persist_config_change(
                controlled_cameras=[*self.app_cfg.controlled_cameras, name],
            )
            if updated is None:
                await self._reply(
                    context,
                    "Failed to save configuration — camera was not added.",
                )
                return
        await self._reply(context, f"Camera '{name}' added to auto-arm.")

    async def _cameras_remove(
        self, context: CallbackContext, name: str
    ) -> None:
        """Remove a camera name from the controlled set and persist."""
        if not name:
            await self._reply(
                context, f"Usage: cameras remove <name>\n\n{_HELP_TEXT}"
            )
            return

        if name not in self.app_cfg.controlled_cameras:
            await self._reply(
                context, f"Camera '{name}' was not in the auto-arm list."
            )
            return

        remaining = [c for c in self.app_cfg.controlled_cameras if c != name]
        updated = self._persist_config_change(controlled_cameras=remaining)
        if updated is None:
            await self._reply(
                context,
                "Failed to save configuration — camera was not removed.",
            )
            return
        await self._reply(context, f"Camera '{name}' removed from auto-arm.")

    async def _cameras_refresh(self, context: CallbackContext) -> None:
        """Force a live Blink refresh (bypassing the main loop's
        periodic cadence) and reconcile controlled_cameras against the
        account's current camera names, replying with a summary of any
        stale camera(s) removed as a result."""
        if not self.blink.is_connected:
            await self._reply(
                context,
                "Not connected to Blink API — cannot refresh cameras.",
            )
            return

        try:
            await self.blink.refresh()
        except Exception:
            _LOGGER.exception(
                "Failed to refresh Blink data via 'cameras refresh'."
            )
            await self._reply(context, "Failed to refresh from Blink API.")
            return

        stale = self.reconcile_stale_cameras()
        if stale:
            await self._reply(
                context,
                f"Refreshed. Removed stale camera(s): {', '.join(stale)}. "
                "If a camera was renamed rather than deleted, re-add it "
                "under its new name: cameras add <name>.",
            )
        else:
            await self._reply(context, "Refreshed. No changes.")

    def reconcile_stale_cameras(self) -> list[str]:
        """Remove any `controlled_cameras` entries no longer present on
        the Blink account (e.g. a camera renamed or deleted via the
        Blink app), clearing their stray per-camera runtime state.

        Called after any refresh — both the main loop's own periodic
        `blink.refresh()` and the on-demand `cameras refresh` command —
        so a silent rename doesn't quietly disable auto-arm coverage
        for that camera without the user noticing. This does NOT try to
        match old name -> new name (e.g. via camera_id); re-adding the
        renamed camera under its new name is a manual follow-up step
        for the user.

        Returns the list of removed camera names (empty if none were
        stale). Callers are responsible for notifying the user via
        whichever channel fits the call site (a proactive message from
        the main loop, or a direct reply from the `cameras refresh`
        command) — this method itself never sends a Telegram message,
        to avoid double-notifying for the same event.
        """
        if not self.blink.is_connected:
            return []

        current_names = {cam.name for cam in self.blink.list_all_cameras()}
        stale = [
            name
            for name in self.app_cfg.controlled_cameras
            if name not in current_names
        ]
        if not stale:
            return []

        remaining = [
            name
            for name in self.app_cfg.controlled_cameras
            if name not in stale
        ]
        updated = self._persist_config_change(controlled_cameras=remaining)
        if updated is None:
            _LOGGER.error(
                "Failed to persist removal of stale camera(s): %s",
                ", ".join(stale),
            )
            return []

        for name in stale:
            self.state.commanded_camera_states.pop(name, None)
            self.state.camera_armed_status.pop(name, None)

        _LOGGER.info(
            "Removed stale camera(s) no longer on Blink account: %s",
            ", ".join(stale),
        )
        return stale

    async def _cmd_ips(self, context: CallbackContext, args: list[str]) -> None:
        """Route 'ips' noun to its list/add/remove verb handler."""
        if not args:
            await self._reply(
                context, f"Usage: ips list|add|remove\n\n{_HELP_TEXT}"
            )
            return

        verb = args[0].lower()
        ip = " ".join(args[1:]).strip()

        if verb == "list":
            await self._ips_list(context)
        elif verb == "add":
            await self._ips_add(context, ip)
        elif verb == "remove":
            await self._ips_remove(context, ip)
        else:
            await self._reply(context, f"Unknown 'ips' subcommand '{verb}'.")

    async def _ips_list(self, context: CallbackContext) -> None:
        """Reply with every monitored IP and its last-known ping status."""
        lines = ["IPs:"]
        for ip in self.app_cfg.monitored_ips:
            online = self.state.ip_ping_status.get(ip)
            status = (
                "online"
                if online
                else "offline" if online is False else "unknown"
            )
            lines.append(f"  {ip} — {status}")
        await self._reply(context, "\n".join(lines))

    async def _ips_add(self, context: CallbackContext, ip: str) -> None:
        """Validate and add an IP to monitoring, notifying the monitor
        only after the config change has been durably persisted."""
        if not ip:
            await self._reply(context, f"Usage: ips add <ip>\n\n{_HELP_TEXT}")
            return

        try:
            ipaddress.ip_address(ip)
        except ValueError:
            await self._reply(context, "Invalid IP address format.")
            return

        if ip not in self.app_cfg.monitored_ips:
            updated = self._persist_config_change(
                monitored_ips=[*self.app_cfg.monitored_ips, ip]
            )
            if updated is None:
                await self._reply(
                    context,
                    "Failed to save configuration — IP was not added.",
                )
                return
            self.monitor.add_ip(ip)
        await self._reply(context, f"IP '{ip}' added to monitoring.")

    async def _ips_remove(self, context: CallbackContext, ip: str) -> None:
        """Remove an IP from monitoring, notifying the monitor only
        after the config change has been durably persisted."""
        if not ip:
            await self._reply(
                context, f"Usage: ips remove <ip>\n\n{_HELP_TEXT}"
            )
            return

        if ip not in self.app_cfg.monitored_ips:
            await self._reply(context, f"IP '{ip}' was not being monitored.")
            return

        remaining = [i for i in self.app_cfg.monitored_ips if i != ip]
        updated = self._persist_config_change(monitored_ips=remaining)
        if updated is None:
            await self._reply(
                context, "Failed to save configuration — IP was not removed."
            )
            return
        self.monitor.remove_ip(ip)
        await self._reply(context, f"IP '{ip}' removed from monitoring.")

    async def _cmd_2fa(self, context: CallbackContext, args: list[str]) -> None:
        """Accept a pending Blink 2FA code and stash it for the main loop."""
        code = " ".join(args).strip()
        if not self.state.is_2fa_pending:
            await self._reply(context, "No 2FA code is currently expected.")
            return
        if not code:
            await self._reply(context, f"Usage: 2fa <code>\n\n{_HELP_TEXT}")
            return
        self.state.received_2fa_code = code
        await self._reply(context, "2FA code received. Processing...")

    async def _cmd_snapshot(
        self, context: CallbackContext, args: list[str]
    ) -> None:
        """Take and send a live snapshot from the named camera."""
        name = " ".join(args).strip()
        if not name:
            await self._reply(
                context, f"Usage: snapshot <name>\n\n{_HELP_TEXT}"
            )
            return

        if not self.blink.is_connected:
            await self._reply(context, "Not connected to Blink API.")
            return

        if not self.blink.has_camera(name):
            await self._reply(context, f"No camera named '{name}' found.")
            return

        image = await self.blink.snapshot(name)
        if image is None:
            await self._reply(
                context, f"Could not get snapshot for camera '{name}'."
            )
            return
        await context.bot.send_photo(chat_id=self.chat_id, photo=image)

    async def _cmd_clip(
        self, context: CallbackContext, args: list[str]
    ) -> None:
        """Send the most recent motion clip from the named camera."""
        name = " ".join(args).strip()
        if not name:
            await self._reply(context, f"Usage: clip <name>\n\n{_HELP_TEXT}")
            return

        if not self.blink.is_connected:
            await self._reply(context, "Not connected to Blink API.")
            return

        if not self.blink.has_camera(name):
            await self._reply(context, f"No camera named '{name}' found.")
            return

        clip = await self.blink.get_latest_clip(name)
        if clip is None:
            await self._reply(
                context, f"No clip available for camera '{name}'."
            )
            return
        await context.bot.send_video(chat_id=self.chat_id, video=clip)

    async def _cmd_alerts(
        self, context: CallbackContext, args: list[str]
    ) -> None:
        """Toggle proactive motion-alert notifications on or off."""
        verb = (args[0].lower() if args else "").strip()
        if verb not in ("on", "off"):
            await self._reply(context, f"Usage: alerts on|off\n\n{_HELP_TEXT}")
            return

        enabled = verb == "on"
        updated = self._persist_config_change(motion_alerts_enabled=enabled)
        if updated is None:
            await self._reply(context, "Failed to save configuration.")
            return
        await self._reply(
            context, f"Motion alerts {'enabled' if enabled else 'disabled'}."
        )

    async def _cmd_settings(
        self, context: CallbackContext, args: list[str]
    ) -> None:
        """Show or update ping_interval_seconds / absence_checks — the
        two mutable numeric settings previously only changeable by
        editing config.json offline."""
        if not args:
            await self._reply(
                context,
                f"Usage: settings show|ping_interval|absence_checks"
                f"\n\n{_HELP_TEXT}",
            )
            return

        verb = args[0].lower()
        rest = args[1:]

        if verb == "show":
            await self._reply(
                context,
                "Settings:\n"
                f"  ping_interval_seconds: "
                f"{self.app_cfg.ping_interval_seconds}\n"
                f"  absence_checks: {self.app_cfg.absence_checks}",
            )
            return

        if verb == "ping_interval":
            await self._settings_set_int(
                context,
                rest,
                field_name="ping_interval_seconds",
                usage="settings ping_interval <seconds>",
                success_label="Ping interval",
            )
            return

        if verb == "absence_checks":
            await self._settings_set_int(
                context,
                rest,
                field_name="absence_checks",
                usage="settings absence_checks <count>",
                success_label="Absence checks",
            )
            return

        await self._reply(context, f"Unknown 'settings' subcommand '{verb}'.")

    async def _settings_set_int(
        self,
        context: CallbackContext,
        args: list[str],
        *,
        field_name: str,
        usage: str,
        success_label: str,
    ) -> None:
        """Parse a single integer argument and persist it to `field_name`
        on AppConfig, relying on Config.save()'s schema validation for
        range checking."""
        if not args:
            await self._reply(context, f"Usage: {usage}\n\n{_HELP_TEXT}")
            return
        try:
            value = int(args[0])
        except ValueError:
            await self._reply(context, f"'{args[0]}' is not an integer.")
            return

        updated = self._persist_config_change(**{field_name: value})
        if updated is None:
            await self._reply(
                context,
                "Failed to save configuration — value rejected or "
                "could not be persisted.",
            )
            return
        await self._reply(context, f"{success_label} set to {value}.")

    # --- Outbound helpers ---

    async def _reply(self, context: CallbackContext, text: str) -> None:
        """Send text to the configured chat via the current update's bot."""
        await context.bot.send_message(chat_id=self.chat_id, text=text)

    async def send_message(self, text: str) -> None:
        """Send a proactive message to the configured chat."""
        if self._application is None:
            _LOGGER.error("Cannot send Telegram message: bot is not started.")
            return
        try:
            await self._application.bot.send_message(
                chat_id=self.chat_id, text=text
            )
        except Exception:
            _LOGGER.exception("Failed to send Telegram message.")

    async def send_photo(self, text: str, photo: bytes) -> None:
        """Send a proactive photo to the configured chat."""
        if self._application is None:
            _LOGGER.error("Cannot send Telegram photo: bot is not started.")
            return
        try:
            await self._application.bot.send_photo(
                chat_id=self.chat_id, photo=photo, caption=text
            )
        except Exception:
            _LOGGER.exception("Failed to send Telegram photo.")

    async def send_video(self, text: str, video: bytes) -> None:
        """Send a proactive video to the configured chat."""
        if self._application is None:
            _LOGGER.error("Cannot send Telegram video: bot is not started.")
            return
        try:
            await self._application.bot.send_video(
                chat_id=self.chat_id, video=video, caption=text
            )
        except Exception:
            _LOGGER.exception("Failed to send Telegram video.")
