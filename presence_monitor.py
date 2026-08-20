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
        # (never checked yet, or the last check could not be executed at
        # all — an OS-level failure, not a timeout; see _ping).
        self._status: dict[str, bool | None] = {}
        # Consecutive-None streak per IP, used to bound how long a
        # genuine "could not run the check at all" result (OSError) can
        # keep giving the benefit of the doubt — see _check_ip.
        self._unknown_streaks: dict[str, int] = {}

    def add_ip(self, ip: str) -> None:
        """Add IP to monitoring set (clears its history)."""
        self._history[ip] = []
        self._status.pop(ip, None)
        self._unknown_streaks.pop(ip, None)

    def remove_ip(self, ip: str) -> None:
        """Remove IP from monitoring set."""
        self._history.pop(ip, None)
        self._status.pop(ip, None)
        self._unknown_streaks.pop(ip, None)

    async def check_all(self) -> Presence:
        """Ping all monitored IPs and return overall Presence.

        Returns Presence.UNKNOWN (never AWAY) when there are no monitored
        IPs — an empty configuration must not be interpreted as "nobody
        home". Otherwise returns HOME if any IP responds (within its
        sliding-window grace period), else AWAY.
        """
        if not self._history:
            return Presence.UNKNOWN

        results = await asyncio.gather(
            *(self._check_ip(ip) for ip in list(self._history))
        )
        return Presence.HOME if any(results) else Presence.AWAY

    def get_status(self) -> dict[str, bool | None]:
        """Return dict of {ip: last_ping_result} for all monitored IPs.

        A value of None means the most recent ping attempt could not be
        executed at all (e.g. missing `ping` binary or other OS-level
        failure) — a genuine "unknown," distinct from True/False, which
        both mean the ping actually completed (with or without a reply,
        including a timeout — a valid, common "no reply" result that
        counts as evidence of absence, not "unknown").
        """
        return dict(self._status)

    # Internal

    def _ping(self, host: str) -> bool | None:
        """Single ping with a hard timeout.

        Returns True if the host replied, False if the ping completed
        without a reply — including a timeout, which is a normal, common
        result for a genuinely offline/unreachable host, not an
        "unknown" — or None only if the ping could not be executed at
        all (missing `ping` binary or other OS-level failure launching
        the subprocess), a true "we don't know" case.
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
            _LOGGER.info(
                "Ping to %s got no reply within %ss — treating as offline.",
                host,
                PING_TIMEOUT_SECONDS,
            )
            return False
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
        attempts). A completed ping with no reply (including a timeout)
        counts as evidence of absence (False), same as an explicit
        ping failure — a "no reply" is itself a valid, actionable signal,
        not an unknown. Only a genuine inability to run the check at all
        (OSError, result is None) is given the benefit of the doubt, and
        even then only for up to `absence_checks` consecutive
        occurrences — beyond that, persistent unknowns are treated as
        absence evidence too, so they can never block AWAY detection
        forever.
        """
        result = self._status[ip] = await self._ping_async(ip)
        history = self._history.setdefault(ip, [])
        if result is None:
            streak = self._unknown_streaks.get(ip, 0) + 1
            value = streak <= self._absence_checks
        else:
            streak = 0
            value = result
        self._unknown_streaks[ip] = streak
        history.append(value)
        del history[: -self._absence_checks]
        return len(history) < self._absence_checks or any(history)
