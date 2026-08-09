import asyncio
import logging
import platform
import subprocess
from enum import Enum

_LOGGER = logging.getLogger(__name__)

PING_TIMEOUT_SECONDS = 5


class Presence(Enum):
    """Overall household presence as determined by PresenceMonitor.

    UNKNOWN is returned when presence cannot be reliably determined (e.g.
    no IPs are configured for monitoring). Callers must treat UNKNOWN as
    "do not change arm state" rather than defaulting to AWAY — an empty
    or failed check must never be interpreted as "nobody home".
    """

    HOME = "home"
    AWAY = "away"
    UNKNOWN = "unknown"


class PresenceMonitor:
    """Tracks ping-based IP presence using a sliding window of attempts."""

    def __init__(self, ips: list[str], absence_checks: int):
        """Initialize with the IPs to monitor and the sliding-window size."""
        self._absence_checks = absence_checks
        self._history: dict[str, list[bool]] = {ip: [] for ip in ips}
        # Raw last-ping result per IP: True/False, or None if unknown
        # (never checked yet, or the last check errored/timed out).
        self._status: dict[str, bool | None] = {}

    def add_ip(self, ip: str) -> None:
        """Add IP to monitoring set (clears its history)."""
        self._history[ip] = []
        self._status.pop(ip, None)

    def remove_ip(self, ip: str) -> None:
        """Remove IP from monitoring set."""
        self._history.pop(ip, None)
        self._status.pop(ip, None)

    async def check_all(self) -> Presence:
        """Ping all monitored IPs and return overall Presence.

        Returns Presence.UNKNOWN (never AWAY) when there are no monitored
        IPs — an empty configuration must not be interpreted as "nobody
        home" (see codereview.md CR-2). Otherwise returns HOME if any IP
        responds (within its sliding-window grace period), else AWAY.
        """
        if not self._history:
            return Presence.UNKNOWN

        results = await asyncio.gather(
            *(self._check_ip(ip) for ip in list(self._history))
        )
        return Presence.HOME if any(results) else Presence.AWAY

    def get_status(self) -> dict[str, bool | None]:
        """Return dict of {ip: last_ping_result} for all monitored IPs.

        A value of None means the IP's last check was never run yet, or
        the most recent ping attempt errored/timed out (unknown, not
        offline).
        """
        return dict(self._status)

    # Internal

    def _ping(self, host: str) -> bool | None:
        """Single ping with a hard timeout.

        Returns True/False for a completed ping, or None if the ping
        could not be completed at all (timeout, missing `ping` binary, or
        other OS-level failure) — an explicit "unknown" result rather than
        a silent False (see codereview.md CR-3).
        """
        param = "-n" if platform.system().lower() == "windows" else "-c"
        command = ["ping", param, "1", host]
        try:
            return (
                subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=PING_TIMEOUT_SECONDS,
                ).returncode
                == 0
            )
        except subprocess.TimeoutExpired:
            _LOGGER.warning(
                "Ping to %s timed out after %ss.", host, PING_TIMEOUT_SECONDS
            )
            return None
        except OSError:
            _LOGGER.exception("Ping to %s failed to execute.", host)
            return None

    async def _ping_async(self, host: str) -> bool | None:
        """Run the blocking ping in an executor thread."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._ping, host)

    async def _check_ip(self, ip: str) -> bool:
        """Sliding window check for one IP.

        Returns True if any of last N pings succeeded (or if fewer than
        N attempts made yet — 'benefit of the doubt' on first N-1
        attempts). A ping that errors/times out (unknown result) is also
        given the benefit of the doubt — it must never count as an
        "absence" strike, since we cannot actually tell whether the
        device is gone or the check itself just failed.
        """
        result = self._status[ip] = await self._ping_async(ip)
        history = self._history.setdefault(ip, [])
        # Unknown (None) results don't count as evidence of absence —
        # treat them as a "present" reading for sliding-window purposes
        # so a flaky ping/timeout can never push presence to AWAY.
        history.append(True if result is None else result)
        del history[: -self._absence_checks]
        return len(history) < self._absence_checks or any(history)
