"""Tests for presence_monitor.py — PresenceMonitor sliding-window pings."""

import subprocess
from unittest.mock import patch

import pytest

from presence_monitor import Presence, PresenceMonitor


@pytest.mark.asyncio
async def test_check_all_all_online_returns_home() -> None:
    monitor = PresenceMonitor(["192.168.0.1", "192.168.0.2"], absence_checks=3)
    with patch.object(PresenceMonitor, "_ping", return_value=True):
        result = await monitor.check_all()
    assert result is Presence.HOME


@pytest.mark.asyncio
async def test_check_all_offline_gives_benefit_of_doubt() -> None:
    monitor = PresenceMonitor(["192.168.0.1"], absence_checks=3)
    with patch.object(PresenceMonitor, "_ping", return_value=False):
        assert await monitor.check_all() is Presence.HOME  # 1st offline
        assert await monitor.check_all() is Presence.HOME  # 2nd offline


@pytest.mark.asyncio
async def test_check_all_returns_away_after_n_consecutive_offline() -> None:
    monitor = PresenceMonitor(["192.168.0.1"], absence_checks=3)
    with patch.object(PresenceMonitor, "_ping", return_value=False):
        await monitor.check_all()  # 1st
        await monitor.check_all()  # 2nd
        result = await monitor.check_all()  # 3rd -> nobody home
    assert result is Presence.AWAY


@pytest.mark.asyncio
async def test_check_all_mixed_results_any_online_ip_keeps_home() -> None:
    monitor = PresenceMonitor(["192.168.0.1", "192.168.0.2"], absence_checks=2)

    def fake_ping(host: str) -> bool:
        return host == "192.168.0.2"

    with patch.object(PresenceMonitor, "_ping", side_effect=fake_ping):
        await monitor.check_all()
        result = await monitor.check_all()

    assert result is Presence.HOME


@pytest.mark.asyncio
async def test_check_all_with_no_monitored_ips_returns_unknown() -> None:
    """An empty monitored-IP list must never be interpreted as 'away' —
    see codereview.md CR-2. Fail closed to 'unknown' instead."""
    monitor = PresenceMonitor([], absence_checks=3)
    result = await monitor.check_all()
    assert result is Presence.UNKNOWN


@pytest.mark.asyncio
async def test_check_all_after_removing_last_ip_returns_unknown() -> None:
    monitor = PresenceMonitor(["192.168.0.1"], absence_checks=3)
    monitor.remove_ip("192.168.0.1")
    result = await monitor.check_all()
    assert result is Presence.UNKNOWN


@pytest.mark.asyncio
async def test_ping_timeout_is_treated_as_unknown_not_offline() -> None:
    """A ping that times out must not count as an absence strike — see
    codereview.md CR-3. It should be given the same benefit of the
    doubt as an unreached absence_checks threshold."""
    monitor = PresenceMonitor(["192.168.0.1"], absence_checks=1)
    with patch(
        "presence_monitor.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="ping", timeout=5),
    ):
        result = await monitor.check_all()
    assert result is Presence.HOME
    assert monitor.get_status()["192.168.0.1"] is None


@pytest.mark.asyncio
async def test_ping_os_error_is_treated_as_unknown_not_offline() -> None:
    """A missing `ping` executable (OSError) must not be treated as
    'offline' — see codereview.md CR-3."""
    monitor = PresenceMonitor(["192.168.0.1"], absence_checks=1)
    with patch(
        "presence_monitor.subprocess.run",
        side_effect=OSError("no such file"),
    ):
        result = await monitor.check_all()
    assert result is Presence.HOME
    assert monitor.get_status()["192.168.0.1"] is None


def test_ping_uses_hard_subprocess_timeout() -> None:
    """_ping must pass an explicit timeout to subprocess.run so a hung
    ping process cannot stall the check indefinitely."""
    monitor = PresenceMonitor(["192.168.0.1"], absence_checks=1)
    with patch("presence_monitor.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        monitor._ping("192.168.0.1")
    _, kwargs = mock_run.call_args
    assert kwargs.get("timeout") is not None


@pytest.mark.asyncio
async def test_add_ip_starts_with_clean_history() -> None:
    monitor = PresenceMonitor(["192.168.0.1"], absence_checks=2)
    with patch.object(PresenceMonitor, "_ping", return_value=False):
        await monitor.check_all()  # 1st offline for existing ip
        await monitor.check_all()  # 2nd offline -> existing ip is False now

    monitor.add_ip("192.168.0.2")

    with patch.object(PresenceMonitor, "_ping", return_value=False):
        # New IP should get "benefit of doubt" on its first reading,
        # regardless of the older IP's already-exhausted history.
        result = await monitor.check_all()

    assert result is Presence.HOME  # new ip still within grace window
    assert monitor.get_status()["192.168.0.2"] is False


def test_remove_ip_drops_it_from_monitoring() -> None:
    monitor = PresenceMonitor(["192.168.0.1", "192.168.0.2"], absence_checks=3)
    monitor.remove_ip("192.168.0.1")
    assert "192.168.0.1" not in monitor.get_status()


@pytest.mark.asyncio
async def test_remove_ip_is_ignored_in_subsequent_check_all() -> None:
    monitor = PresenceMonitor(["192.168.0.1", "192.168.0.2"], absence_checks=2)
    monitor.remove_ip("192.168.0.1")

    with patch.object(
        PresenceMonitor, "_ping", return_value=False
    ) as mock_ping:
        await monitor.check_all()

    called_hosts = {call.args[0] for call in mock_ping.call_args_list}
    assert called_hosts == {"192.168.0.2"}


@pytest.mark.asyncio
async def test_get_status_returns_current_per_ip_dict() -> None:
    monitor = PresenceMonitor(["192.168.0.1", "192.168.0.2"], absence_checks=2)

    def fake_ping(host: str) -> bool:
        return host == "192.168.0.1"

    with patch.object(PresenceMonitor, "_ping", side_effect=fake_ping):
        await monitor.check_all()

    assert monitor.get_status() == {
        "192.168.0.1": True,
        "192.168.0.2": False,
    }


def test_get_status_before_any_check_has_no_readings_yet() -> None:
    monitor = PresenceMonitor(["192.168.0.1"], absence_checks=3)
    # Before any check_all(), status dict exists but has no results.
    assert monitor.get_status() == {}
