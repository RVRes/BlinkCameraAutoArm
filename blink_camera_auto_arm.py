import asyncio
import contextlib
import logging
import os
import random
import signal
import sys
import time
from dataclasses import dataclass, field

from dotenv import load_dotenv

from blink_service import BlinkService, ConnectResult
from config import AppConfig, Config
from presence_monitor import Presence, PresenceMonitor
from state import AppState
from telegram_bot import TelegramBot

_LOGGER = logging.getLogger(__name__)

# Backoff bounds for repeated Blink connection failures (codereview.md
# M-1) — capped exponential backoff with jitter, reset after success.
_BACKOFF_BASE_SECONDS = 30
_BACKOFF_MAX_SECONDS = 1800
# Minimum time between repeated "still can't connect" notifications while
# backed off, so a prolonged outage doesn't spam Telegram every retry.
_CONNECT_FAILURE_NOTIFY_INTERVAL_SECONDS = 900


@dataclass
class LoopContext:
    """Per-run scratch state for the main loop that must survive across
    iterations but is not part of AppState (not read by the Telegram bot,
    not user-facing).
    """

    last_motion_seen: dict[str, str | None] = field(default_factory=dict)
    motion_alerts_primed: bool = False
    # Connection-failure backoff/notification bookkeeping (M-1).
    connect_failure_count: int = 0
    next_connect_attempt_time: float = 0.0
    last_connect_failure_notify_time: float = 0.0
    # Last-observed presence, for transition-only logging (codereview.md
    # L-3) — avoids repeating an unchanged "away"/"home" line every
    # single iteration while still logging every actual change.
    last_presence: Presence | None = None


def _connect_backoff_seconds(failure_count: int) -> float:
    """Capped exponential backoff with jitter for connection retries."""
    backoff = min(
        _BACKOFF_BASE_SECONDS * (2 ** max(failure_count - 1, 0)),
        _BACKOFF_MAX_SECONDS,
    )
    jitter = random.uniform(0, backoff * 0.25)
    return backoff + jitter


async def run_iteration(
    cfg: AppConfig,
    state: AppState,
    blink: BlinkService,
    monitor: PresenceMonitor,
    bot: TelegramBot,
    ctx: LoopContext,
) -> None:
    """Run a single main-loop iteration. See PLAN.md's main loop
    pseudocode for the authoritative description of this logic.
    """
    if not state.is_app_enabled:
        return

    # Health signal (codereview.md L-3): record that this iteration
    # actually ran, regardless of what it does below, so /camerabot
    # status can distinguish a live-but-wedged process from one that's
    # genuinely making progress.
    state.time_of_last_iteration = time.time()

    # --- 2FA completion ---
    if state.is_2fa_pending:
        if not state.received_2fa_code:
            # A 2FA challenge is outstanding and no code has arrived yet.
            # Do NOT fall through to connect() — that would replace the
            # Blink object holding the pending challenge state, silently
            # invalidating it and forcing a fresh 2FA request (see
            # codereview.md CR-1). Simply wait for the next iteration.
            return
        code = state.received_2fa_code
        state.received_2fa_code = None
        state.is_2fa_pending = False
        success = await blink.submit_2fa_code(code)
        if success:
            await blink.save_credentials()
            _LOGGER.info("2FA accepted; connected to Blink API.")
            await bot.send_message("2FA accepted. Connected.")
        else:
            # Keep is_2fa_pending False->the user must trigger a fresh
            # connect()/challenge on the next iteration rather than
            # silently retrying against a potentially stale challenge.
            _LOGGER.warning("2FA code rejected by Blink API.")
            await bot.send_message("2FA failed. Check code and try again.")
        return

    # --- Connection (re)establishment ---
    if not blink.is_connected:
        now = time.time()
        if now < ctx.next_connect_attempt_time:
            return  # still backing off from a recent failure
        result = await blink.connect()
        if result == ConnectResult.OK:
            ctx.connect_failure_count = 0
            ctx.next_connect_attempt_time = 0.0
            await blink.save_credentials()
            _LOGGER.info("Connected to Blink API.")
            await bot.send_message("Connected to Blink API.")
        elif result == ConnectResult.NEEDS_2FA:
            ctx.connect_failure_count = 0
            ctx.next_connect_attempt_time = 0.0
            state.is_2fa_pending = True
            _LOGGER.info("Blink API requires 2FA.")
            await bot.send_message("2FA required. Send: /camerabot 2fa <code>")
            return
        else:  # ConnectResult.FAILED
            ctx.connect_failure_count += 1
            backoff = _connect_backoff_seconds(ctx.connect_failure_count)
            ctx.next_connect_attempt_time = now + backoff
            _LOGGER.error(
                "Could not connect to Blink API (failure #%d). Will "
                "retry in %.0fs.",
                ctx.connect_failure_count,
                backoff,
            )
            if (
                now - ctx.last_connect_failure_notify_time
                >= _CONNECT_FAILURE_NOTIFY_INTERVAL_SECONDS
            ):
                ctx.last_connect_failure_notify_time = now
                await bot.send_message(
                    "Could not connect to Blink API "
                    f"(failure #{ctx.connect_failure_count}). Retrying "
                    f"in the background."
                )
            return  # do NOT disable the app — retry next iteration

    # --- Periodic refresh ---
    try:
        await blink.refresh()
    except Exception:
        _LOGGER.exception("Failed to refresh Blink data.")
        return

    # --- Update camera status cache (display only) ---
    for cam in blink.list_all_cameras():
        state.camera_armed_status[cam.name] = cam.armed
        expected = state.commanded_camera_states.get(cam.name)
        if expected is not None and cam.armed != expected:
            _LOGGER.warning(
                "Camera '%s': commanded=%s but API reports armed=%s",
                cam.name,
                expected,
                cam.armed,
            )

    # --- Presence check ---
    presence = await monitor.check_all()
    state.ip_ping_status = monitor.get_status()

    if presence is not ctx.last_presence:
        # Log every actual presence transition (codereview.md L-3), not
        # every iteration's unchanged reading — keeps logs scannable for
        # router diagnosis while still surfacing every state change.
        _LOGGER.info(
            "Presence transition: %s -> %s.",
            ctx.last_presence.value if ctx.last_presence else "startup",
            presence.value,
        )
        ctx.last_presence = presence

    if presence is Presence.UNKNOWN:
        # No monitored IPs (or no reliable reading yet) — never treat
        # this as "nobody home". Skip auto arm/disarm entirely rather
        # than defaulting to away (see codereview.md CR-2).
        if cfg.controlled_cameras:
            _LOGGER.warning(
                "Presence is unknown (no monitored IPs configured?) — "
                "skipping auto arm/disarm this iteration."
            )
    else:
        someone_home = presence is Presence.HOME
        # --- Auto arm/disarm ---
        for camera_name in cfg.controlled_cameras:
            currently_commanded = state.commanded_camera_states.get(camera_name)
            try:
                if not someone_home and currently_commanded is not True:
                    results = await blink.arm_cameras([camera_name])
                    if results.get(camera_name):
                        state.commanded_camera_states[camera_name] = True
                        state.time_of_last_arm_change = time.time()
                        _LOGGER.info(
                            "Presence=away — armed camera '%s'.",
                            camera_name,
                        )
                        await bot.send_message(
                            f"Nobody home. Arming: {camera_name}."
                        )
                    else:
                        _LOGGER.warning(
                            "Controlled camera '%s' not found in Blink "
                            "account — skipping.",
                            camera_name,
                        )
                elif someone_home and currently_commanded is not False:
                    results = await blink.disarm_cameras([camera_name])
                    if results.get(camera_name):
                        state.commanded_camera_states[camera_name] = False
                        state.time_of_last_arm_change = time.time()
                        _LOGGER.info(
                            "Presence=home — disarmed camera '%s'.",
                            camera_name,
                        )
                        await bot.send_message(
                            f"Someone home. Disarming: {camera_name}."
                        )
                    else:
                        _LOGGER.warning(
                            "Controlled camera '%s' not found in Blink "
                            "account — skipping.",
                            camera_name,
                        )
            except Exception:
                _LOGGER.exception(
                    "Failed to arm/disarm camera '%s'.", camera_name
                )
                continue

    # --- Motion alerts ---
    if cfg.motion_alerts_enabled:
        # Scope motion polling to controlled cameras only — matches the
        # documented behavior (see codereview.md M-2). An uncontrolled
        # camera must never generate a proactive alert.
        events = await blink.get_new_motion_events(
            ctx.last_motion_seen, camera_names=cfg.controlled_cameras
        )
        should_send = ctx.motion_alerts_primed
        for event in events:
            ctx.last_motion_seen[event.camera_name] = event.clip_time
        ctx.motion_alerts_primed = True
        if should_send:
            for event in events:
                msg = (
                    f"Motion detected: {event.camera_name} at "
                    f"{event.clip_time}."
                )
                if event.clip_bytes:
                    await bot.send_video(msg, event.clip_bytes)
                else:
                    await bot.send_message(msg)


async def run_main_loop(
    cfg: AppConfig,
    state: AppState,
    blink: BlinkService,
    monitor: PresenceMonitor,
    bot: TelegramBot,
) -> None:
    """Main control loop. Runs concurrently with bot.start() as a sibling
    asyncio task; both are expected to run until cancelled by main()'s
    shutdown sequence.
    """
    ctx = LoopContext()
    while True:
        await asyncio.sleep(cfg.ping_interval_seconds)
        try:
            await run_iteration(cfg, state, blink, monitor, bot, ctx)
        except Exception:
            _LOGGER.exception("Unhandled error in main loop iteration.")


async def main() -> None:
    """Load config/state, wire up services, and run the app until
    stopped (SIGINT/SIGTERM triggers a graceful shutdown)."""
    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), stream=sys.stdout)

    config = Config()
    cfg = config.load()
    state = AppState()

    monitor = PresenceMonitor(cfg.monitored_ips, cfg.absence_checks)
    blink = BlinkService(cfg.blink_username, cfg.blink_password)
    bot = TelegramBot(
        token=cfg.telegram_bot_token,
        chat_id=cfg.telegram_chat_id,
        allowed_user_id=cfg.telegram_allowed_user_id,
        config=config,
        app_cfg=cfg,
        state=state,
        blink=blink,
        monitor=monitor,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_shutdown() -> None:
        _LOGGER.info("Shutdown signal received.")
        stop_event.set()

    # loop.add_signal_handler is POSIX-only (the router deployment
    # target); on platforms without it (e.g. Windows dev machines) we
    # fall back to default KeyboardInterrupt/terminate handling.
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_shutdown)

    main_loop_task = asyncio.create_task(
        run_main_loop(cfg, state, blink, monitor, bot)
    )
    bot_task = asyncio.create_task(bot.start())
    stop_task = asyncio.create_task(stop_event.wait())

    try:
        done, pending = await asyncio.wait(
            {main_loop_task, bot_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            if task is stop_task:
                continue
            exc = task.exception()
            if exc is not None:
                raise exc
    finally:
        for task in (main_loop_task, bot_task, stop_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            main_loop_task, bot_task, stop_task, return_exceptions=True
        )
        await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
