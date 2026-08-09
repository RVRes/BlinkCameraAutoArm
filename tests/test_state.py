"""Tests for state.py — AppState runtime-only cross-task signals."""

import asyncio

from state import AppState


def test_defaults() -> None:
    state = AppState()
    assert state.is_app_enabled is True
    assert state.is_2fa_pending is False
    assert state.received_2fa_code is None
    assert state.ip_ping_status == {}
    assert state.camera_armed_status == {}
    assert state.commanded_camera_states == {}
    assert state.time_of_last_arm_change is None
    assert state.time_of_last_iteration is None


def test_fields_are_mutable_and_readable() -> None:
    state = AppState()

    state.is_app_enabled = False
    assert state.is_app_enabled is False

    state.is_2fa_pending = True
    state.received_2fa_code = "123456"
    assert state.is_2fa_pending is True
    assert state.received_2fa_code == "123456"

    state.ip_ping_status["192.168.0.1"] = True
    assert state.ip_ping_status == {"192.168.0.1": True}

    state.camera_armed_status["Backyard"] = True
    assert state.camera_armed_status == {"Backyard": True}

    state.commanded_camera_states["Backyard"] = False
    assert state.commanded_camera_states == {"Backyard": False}

    state.time_of_last_arm_change = 123.0
    assert state.time_of_last_arm_change == 123.0

    state.time_of_last_iteration = 456.0
    assert state.time_of_last_iteration == 456.0


def test_each_instance_has_independent_mutable_defaults() -> None:
    """Dict fields must use default_factory — no shared mutable state."""
    state_a = AppState()
    state_b = AppState()

    state_a.ip_ping_status["192.168.0.1"] = True
    assert state_b.ip_ping_status == {}

    state_a.camera_armed_status["Backyard"] = True
    assert state_b.camera_armed_status == {}

    state_a.commanded_camera_states["Backyard"] = True
    assert state_b.commanded_camera_states == {}


async def test_concurrent_coroutines_share_same_state_instance() -> None:
    """AppState mutated from multiple coroutines does not corrupt state,
    since asyncio is single-threaded and mutation is cooperative."""
    state = AppState()

    async def writer(ip: str, value: bool) -> None:
        for _ in range(50):
            state.ip_ping_status[ip] = value
            await asyncio.sleep(0)

    await asyncio.gather(
        writer("192.168.0.1", True),
        writer("192.168.0.2", False),
        writer("192.168.0.3", True),
    )

    assert state.ip_ping_status == {
        "192.168.0.1": True,
        "192.168.0.2": False,
        "192.168.0.3": True,
    }
