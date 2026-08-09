import asyncio
import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.blinkpy import Blink
from blinkpy.helpers.util import json_load

_LOGGER = logging.getLogger(__name__)

# Absolute path so the credentials cache is always found/created next to
# this module, regardless of the process's current working directory
# (e.g. a cron watchdog that does not `cd` first). See codereview.md CR-4.
APP_DIR = Path(__file__).resolve().parent
DEFAULT_CREDENTIALS_FILE = APP_DIR / "blink_credentials.json"

# Hard deadline for any single Blink API call. A stuck network call must
# not block the main loop indefinitely — see codereview.md H-2.
BLINK_CALL_TIMEOUT_SECONDS = 30


class ConnectResult(Enum):
    """Outcome of a BlinkService.connect() attempt."""

    OK = "ok"
    NEEDS_2FA = "needs_2fa"
    FAILED = "failed"


class BlinkTimeoutError(Exception):
    """Raised when a Blink API call exceeds BLINK_CALL_TIMEOUT_SECONDS."""


@dataclass
class CameraInfo:
    """Snapshot of a single Blink camera's identity and status."""

    name: str
    camera_id: str
    network_id: str
    product_type: str
    online: bool
    armed: bool
    battery: str | None


@dataclass
class MotionEvent:
    """A single detected-motion clip for a camera."""

    camera_name: str
    clip_time: str
    clip_bytes: bytes | None


class BlinkService:
    """Correct async wrapper around blinkpy 0.25.x (OAuth2+PKCE auth).

    All methods that touch the underlying `Blink`/session object acquire
    an internal asyncio.Lock, serializing access between the main loop
    and concurrent Telegram command handlers (snapshot/clip/etc.) — see
    codereview.md H-1. Each call is also bounded by
    BLINK_CALL_TIMEOUT_SECONDS so a hung network call cannot stall the
    main loop indefinitely (H-2).
    """

    CREDENTIALS_FILE = str(DEFAULT_CREDENTIALS_FILE)

    def __init__(self, username: str, password: str):
        """Store Blink account credentials; connection is lazy via connect()."""
        self._username = username
        self._password = password
        self._blink: Blink | None = None
        self._lock = asyncio.Lock()

    async def _with_timeout(self, coro, operation: str):
        """Await `coro` with a hard deadline, converting timeout into a
        BlinkTimeoutError so callers can distinguish it from other
        failures without swallowing cancellation."""
        try:
            async with asyncio.timeout(BLINK_CALL_TIMEOUT_SECONDS):
                return await coro
        except TimeoutError as e:
            raise BlinkTimeoutError(
                f"Blink API call '{operation}' timed out after "
                f"{BLINK_CALL_TIMEOUT_SECONDS}s."
            ) from e

    # --- Authentication ---

    async def connect(self) -> ConnectResult:
        """Attempt connection to Blink API.

        Single-step flow: load saved credentials if present, merge with
        username+password from AppConfig (always present so blinkpy's
        internal refresh->fresh-login fallback has credentials to use),
        construct Blink + Auth, call blink.start().
        """
        async with self._lock:
            saved_data = {}
            if os.path.exists(self.CREDENTIALS_FILE):
                saved_data = await json_load(self.CREDENTIALS_FILE) or {}

            login_data = {
                **saved_data,
                "username": self._username,
                "password": self._password,
            }

            blink = Blink()
            blink.auth = Auth(login_data, no_prompt=True)
            self._blink = blink

            try:
                success = await self._with_timeout(blink.start(), "connect")
            except BlinkTwoFARequiredError:
                return ConnectResult.NEEDS_2FA

            return ConnectResult.OK if success else ConnectResult.FAILED

    async def submit_2fa_code(self, code: str) -> bool:
        """Complete 2FA via Blink.send_2fa_code(code)."""
        async with self._lock:
            return await self._with_timeout(
                self._blink.send_2fa_code(code), "submit_2fa_code"
            )

    async def save_credentials(self) -> None:
        """Save blink login attributes to CREDENTIALS_FILE, then restrict
        file permissions to owner-only (contains account tokens — see
        codereview.md H-5)."""
        async with self._lock:
            await self._with_timeout(
                self._blink.save(self.CREDENTIALS_FILE), "save_credentials"
            )
        _restrict_permissions(self.CREDENTIALS_FILE)

    # --- Camera discovery & status ---

    def list_all_cameras(self) -> list[CameraInfo]:
        """Return info for every camera known to the Blink account."""
        cameras = []
        for cam in self._blink.cameras.values():
            armed = cam.arm if isinstance(cam.arm, bool) else False
            cameras.append(
                CameraInfo(
                    name=cam.name,
                    camera_id=cam.camera_id,
                    network_id=cam.network_id,
                    product_type=cam.product_type,
                    online=cam.online,
                    armed=armed,
                    battery=cam.battery,
                )
            )
        return cameras

    async def refresh(self) -> None:
        """Call blink.refresh(). Respects blinkpy's built-in throttle."""
        async with self._lock:
            await self._with_timeout(self._blink.refresh(), "refresh")

    # --- Arm/disarm (per-camera motion detection) ---

    async def arm_cameras(self, names: list[str]) -> dict[str, bool]:
        """Arm (enable motion detection) for each named camera."""
        return await self._set_cameras_armed(names, True)

    async def disarm_cameras(self, names: list[str]) -> dict[str, bool]:
        """Disarm (disable motion detection) for each named camera."""
        return await self._set_cameras_armed(names, False)

    async def _set_cameras_armed(
        self, names: list[str], armed: bool
    ) -> dict[str, bool]:
        """Set arm state for each named camera.

        Returns {name: success} — True if the camera was found and the
        command was issued, False if the camera name is unknown. This is
        a success flag, NOT the resulting armed state (which would make
        disarm's success indistinguishable from a not-found camera, since
        both would read as False).
        """
        async with self._lock:
            results: dict[str, bool] = {}
            for name in names:
                camera = self._blink.cameras.get(name)
                if camera is None:
                    results[name] = False
                    continue
                await self._with_timeout(
                    camera.async_arm(armed), f"arm_camera:{name}"
                )
                results[name] = True
            return results

    # --- On-demand media (independent of auto-arm loop) ---

    async def snapshot(self, camera_name: str) -> bytes | None:
        """Trigger snap_picture() for named camera."""
        async with self._lock:
            camera = self._blink.cameras.get(camera_name)
            if camera is None:
                return None
            await self._with_timeout(camera.snap_picture(), "snapshot")
            return camera.image_from_cache

    async def get_latest_clip(self, camera_name: str) -> bytes | None:
        """Return bytes of most recent motion clip for camera."""
        async with self._lock:
            camera = self._blink.cameras.get(camera_name)
            if camera is None or not camera.recent_clips:
                return None
            latest = max(camera.recent_clips, key=lambda c: c["time"])
            return await self._download_clip(camera, latest["clip"])

    # --- Motion alert polling ---

    async def get_new_motion_events(
        self,
        last_seen: dict[str, str | None],
        camera_names: list[str] | None = None,
    ) -> list[MotionEvent]:
        """Compare camera.recent_clips against last_seen timestamps.

        If `camera_names` is given, only those cameras are considered —
        used to scope proactive motion alerts to the controlled-camera
        allowlist rather than every camera on the account (see
        codereview.md M-2). When omitted, all account cameras are
        checked (used by callers that want the full account view).
        """
        async with self._lock:
            events: list[MotionEvent] = []
            for camera in self._blink.cameras.values():
                if camera_names is not None and camera.name not in camera_names:
                    continue
                baseline = last_seen.get(camera.name)
                new_clips = [
                    clip
                    for clip in camera.recent_clips
                    if baseline is None or clip["time"] > baseline
                ]
                for clip in sorted(new_clips, key=lambda c: c["time"]):
                    clip_bytes = await self._download_clip(camera, clip["clip"])
                    events.append(
                        MotionEvent(
                            camera_name=camera.name,
                            clip_time=clip["time"],
                            clip_bytes=clip_bytes,
                        )
                    )
            return events

    async def _download_clip(self, camera, url: str) -> bytes | None:
        """Fetch a motion clip's bytes from its URL, or None on failure."""
        response = await self._with_timeout(
            camera.get_video_clip(url=url), "download_clip"
        )
        if response and response.status == 200:
            return await response.read()
        return None

    # --- Internal ---

    @property
    def is_connected(self) -> bool:
        """True when blink.cameras is populated."""
        return bool(self._blink and self._blink.cameras)


def _restrict_permissions(path: str) -> None:
    """Best-effort restriction of a file's permissions to owner-only.

    No-op on Windows, where POSIX mode bits are not meaningfully
    enforced; the router deployment target is embedded Linux.
    """
    if os.name != "posix":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        _LOGGER.warning("Could not restrict permissions on '%s'.", path)
