import asyncio
import logging
import os
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import TypeVar

from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.blinkpy import Blink
from blinkpy.helpers.util import json_load

_LOGGER = logging.getLogger(__name__)

# Absolute path so the credentials cache is always found/created next to
# this module, regardless of the process's current working directory
# (e.g. a cron watchdog that does not `cd` first).
APP_DIR = Path(__file__).resolve().parent
DEFAULT_CREDENTIALS_FILE = APP_DIR / "blink_credentials.json"

# Hard deadline for any single Blink API call. A stuck network call must
# not block the main loop indefinitely.
BLINK_CALL_TIMEOUT_SECONDS = 30

# How far back get_latest_clip() looks in Blink's cloud video history
# when searching for a camera's most recent clip, and how many ~25-item
# pages of that history it is willing to page through. Generous enough
# to find a clip even if it's been a while since the last motion event,
# without paging indefinitely.
CLIP_LOOKUP_LOOKBACK_DAYS = 7
CLIP_LOOKUP_MAX_PAGES = 10

T = TypeVar("T")


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
    and concurrent Telegram command handlers (snapshot/clip/etc.). Each
    call is also bounded by BLINK_CALL_TIMEOUT_SECONDS so a hung network
    call cannot stall the main loop indefinitely.
    """

    CREDENTIALS_FILE = str(DEFAULT_CREDENTIALS_FILE)

    def __init__(self, username: str, password: str):
        """Store Blink account credentials; connection is lazy via connect()."""
        self._username = username
        self._password = password
        self._blink: Blink | None = None
        self._lock = asyncio.Lock()

    async def _with_timeout(self, coro: Awaitable[T], operation: str) -> T:
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
            blink = self._require_blink()
            return await self._with_timeout(
                blink.send_2fa_code(code), "submit_2fa_code"
            )

    async def save_credentials(self) -> None:
        """Save blink login attributes to CREDENTIALS_FILE, then restrict
        file permissions to owner-only (contains account tokens)."""
        async with self._lock:
            blink = self._require_blink()
            await self._with_timeout(
                blink.save(self.CREDENTIALS_FILE), "save_credentials"
            )
        _restrict_permissions(self.CREDENTIALS_FILE)

    # --- Camera discovery & status ---

    def list_all_cameras(self) -> list[CameraInfo]:
        """Return info for every camera known to the Blink account."""
        blink = self._require_blink()
        cameras = []
        for cam in blink.cameras.values():
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
            blink = self._require_blink()
            await self._with_timeout(blink.refresh(), "refresh")

    def has_camera(self, camera_name: str) -> bool:
        """True if `camera_name` exists on the connected Blink account.

        Lets callers (e.g. the Telegram bot) distinguish "camera not
        found" from "camera found but has no snapshot/clip available
        yet" before calling snapshot()/get_latest_clip(), which both
        return None for either situation.
        """
        blink = self._require_blink()
        return camera_name in blink.cameras

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
            blink = self._require_blink()
            results: dict[str, bool] = {}
            for name in names:
                camera = blink.cameras.get(name)
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
            blink = self._require_blink()
            camera = blink.cameras.get(camera_name)
            if camera is None:
                return None
            await self._with_timeout(camera.snap_picture(), "snapshot")
            return camera.image_from_cache

    async def get_latest_clip(self, camera_name: str) -> bytes | None:
        """Return bytes of the most recent motion clip for `camera_name`,
        queried live from Blink's cloud video history rather than the
        local `camera.recent_clips` cache.

        `camera.recent_clips` is only populated opportunistically during
        `refresh()`'s own `update_images()` cycle, and entries are
        pruned/removed by blinkpy shortly after — so relying on it here
        makes on-demand clip requests racy against the main loop's own
        refresh/expiry cadence (a clip that existed a minute ago may
        already be gone from the cache, even though it's still present
        in Blink's cloud history). Querying
        `Blink.get_videos_metadata()` (Blink's `/media/changed` video
        history endpoint) directly avoids that race, at the cost of one
        extra API call per on-demand request.
        """
        async with self._lock:
            blink = self._require_blink()
            camera = blink.cameras.get(camera_name)
            if camera is None:
                return None
            since = (
                datetime.now(timezone.utc)
                - timedelta(days=CLIP_LOOKUP_LOOKBACK_DAYS)
            ).strftime("%Y/%m/%d %H:%M:%S")
            videos = await self._with_timeout(
                blink.get_videos_metadata(
                    since=since, stop=CLIP_LOOKUP_MAX_PAGES
                ),
                "get_latest_clip:list_videos",
            )
            camera_videos = [
                video
                for video in videos
                if video.get("device_name") == camera_name
                and not video.get("deleted")
            ]
            if not camera_videos:
                return None
            latest = max(camera_videos, key=lambda v: v["created_at"])
            url = f"{blink.urls.base_url}{latest['media']}"
            return await self._download_clip(camera, url)

    # --- Motion alert polling ---

    async def get_new_motion_events(
        self,
        last_seen: dict[str, str | None],
        camera_names: list[str] | None = None,
    ) -> list[MotionEvent]:
        """Compare camera.recent_clips against last_seen timestamps.

        If `camera_names` is given, only those cameras are considered —
        used to scope proactive motion alerts to the controlled-camera
        allowlist rather than every camera on the account. When omitted,
        all account cameras are checked (used by callers that want the
        full account view).
        """
        async with self._lock:
            blink = self._require_blink()
            events: list[MotionEvent] = []
            for camera in blink.cameras.values():
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

    def _require_blink(self) -> Blink:
        """Return the active Blink session or report that none exists."""
        if self._blink is None:
            raise RuntimeError("Blink service is not connected.")
        return self._blink

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
