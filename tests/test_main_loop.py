"""Tests for blink_camera_auto_arm.py main loop logic."""

import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from blink_camera_auto_arm import LoopContext, _configure_logging, run_iteration
from blink_service import ConnectResult, MotionEvent
from config import AppConfig
from presence_monitor import Presence
from state import AppState


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig(
        blink_username="test@example.com",
        blink_password="password",
        telegram_bot_token="token",
        telegram_chat_id=100,
        telegram_allowed_user_id=999,
        monitored_ips=["192.168.0.1"],
        controlled_cameras=["Backyard"],
        absence_checks=3,
        ping_interval_seconds=60,
        motion_alerts_enabled=False,
    )


@pytest.fixture
def app_state() -> AppState:
    return AppState()


@pytest.fixture
def mock_blink() -> MagicMock:
    svc = MagicMock()
    svc.is_connected = True
    svc.connect = AsyncMock(return_value=ConnectResult.OK)
    svc.submit_2fa_code = AsyncMock(return_value=True)
    svc.save_credentials = AsyncMock()
    svc.refresh = AsyncMock()
    svc.list_all_cameras = MagicMock(return_value=[])
    svc.arm_cameras = AsyncMock(return_value={"Backyard": True})
    svc.disarm_cameras = AsyncMock(return_value={"Backyard": True})
    svc.get_new_motion_events = AsyncMock(return_value=[])
    return svc


@pytest.fixture
def mock_monitor() -> MagicMock:
    mon = MagicMock()
    mon.check_all = AsyncMock(return_value=Presence.HOME)
    mon.get_status = MagicMock(return_value={})
    return mon


@pytest.fixture
def mock_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    bot.send_video = AsyncMock()
    bot.reconcile_stale_cameras = MagicMock(return_value=[])
    return bot


@pytest.fixture
def ctx() -> LoopContext:
    return LoopContext()


async def _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx):
    await run_iteration(
        cfg=app_config,
        state=app_state,
        blink=mock_blink,
        monitor=mock_monitor,
        bot=mock_bot,
        ctx=ctx,
    )


@pytest.mark.asyncio
async def test_nobody_home_arms_cameras(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    mock_monitor.check_all.return_value = Presence.AWAY

    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    mock_blink.arm_cameras.assert_awaited_once_with(["Backyard"])
    assert app_state.commanded_camera_states["Backyard"] is True


@pytest.mark.asyncio
async def test_nobody_home_already_armed_no_redundant_call(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    mock_monitor.check_all.return_value = Presence.AWAY
    app_state.commanded_camera_states["Backyard"] = True

    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    mock_blink.arm_cameras.assert_not_awaited()


@pytest.mark.asyncio
async def test_someone_home_disarms_cameras(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    mock_monitor.check_all.return_value = Presence.HOME
    app_state.commanded_camera_states["Backyard"] = True

    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    mock_blink.disarm_cameras.assert_awaited_once_with(["Backyard"])
    assert app_state.commanded_camera_states["Backyard"] is False


@pytest.mark.asyncio
async def test_someone_home_already_disarmed_no_redundant_call(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    mock_monitor.check_all.return_value = Presence.HOME
    app_state.commanded_camera_states["Backyard"] = False

    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    mock_blink.disarm_cameras.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_presence_skips_arm_disarm(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    """Unknown presence (e.g. no monitored IPs) must never be treated
    as 'away' — no arm/disarm action should be taken at all."""
    mock_monitor.check_all.return_value = Presence.UNKNOWN

    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    mock_blink.arm_cameras.assert_not_awaited()
    mock_blink.disarm_cameras.assert_not_awaited()
    assert "Backyard" not in app_state.commanded_camera_states


@pytest.mark.asyncio
async def test_app_disabled_skips_everything(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    app_state.is_app_enabled = False

    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    mock_blink.arm_cameras.assert_not_awaited()
    mock_blink.disarm_cameras.assert_not_awaited()
    mock_blink.refresh.assert_not_awaited()
    mock_monitor.check_all.assert_not_awaited()


# --- Stale camera reconciliation (item 9) ---


@pytest.mark.asyncio
async def test_periodic_refresh_runs_stale_camera_reconciliation(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    """Every periodic refresh must call the bot's reconciliation helper
    so a camera renamed/deleted on the Blink side is caught within one
    interval without the user ever running /cambot cameras refresh."""
    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    mock_bot.reconcile_stale_cameras.assert_called_once_with()


@pytest.mark.asyncio
async def test_stale_camera_removal_sends_proactive_notification(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    """When reconciliation actually removes stale camera(s), the main
    loop must proactively notify the user via Telegram."""
    mock_bot.reconcile_stale_cameras.return_value = ["Old Camera"]

    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    sent_texts = [
        call.args[0] for call in mock_bot.send_message.await_args_list
    ]
    assert any("Old Camera" in text for text in sent_texts)


@pytest.mark.asyncio
async def test_no_stale_cameras_sends_no_reconciliation_notification(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    """When nothing is stale, no reconciliation-specific message is
    sent (distinct from other main-loop notifications, which are
    unaffected by this test's assertions)."""
    mock_bot.reconcile_stale_cameras.return_value = []

    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    sent_texts = [
        call.args[0] for call in mock_bot.send_message.await_args_list
    ]
    assert not any("removed from auto-arm" in text for text in sent_texts)


@pytest.mark.asyncio
async def test_manual_arm_not_overridden_while_still_away(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    """A camera manually armed (e.g. via /cambot arm, which sets
    commanded_camera_states exactly as run_iteration()'s own auto-arm
    branch does) must not be re-armed redundantly by the main loop on
    its next tick while presence is still away."""
    mock_monitor.check_all.return_value = Presence.AWAY
    app_state.commanded_camera_states["Backyard"] = True
    app_state.time_of_last_arm_change = time.time()

    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    mock_blink.arm_cameras.assert_not_awaited()
    assert app_state.commanded_camera_states["Backyard"] is True


@pytest.mark.asyncio
async def test_manual_arm_overridden_by_presence_home(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    """A manually-armed camera IS disarmed by the main loop the moment
    presence flips to home — presence-based disarm always takes
    precedence over a stale manual arm."""
    app_state.commanded_camera_states["Backyard"] = True
    mock_monitor.check_all.return_value = Presence.HOME

    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    mock_blink.disarm_cameras.assert_awaited_once_with(["Backyard"])
    assert app_state.commanded_camera_states["Backyard"] is False


@pytest.mark.asyncio
async def test_manual_arm_while_disabled_then_reenable_home_disarms(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    """Per item 6's acceptance criteria: after a manual arm, disabling
    the app, and presence becoming home while disabled, cameras are NOT
    auto-disarmed (the loop isn't running). Re-enabling while someone
    is home DOES disarm them on the next iteration."""
    # Manual arm (simulating /cambot arm's state update).
    app_state.commanded_camera_states["Backyard"] = True
    app_state.time_of_last_arm_change = time.time()

    # Disable — main loop must not run at all.
    app_state.is_app_enabled = False
    mock_monitor.check_all.return_value = Presence.HOME
    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)
    mock_blink.disarm_cameras.assert_not_awaited()
    assert app_state.commanded_camera_states["Backyard"] is True

    # Re-enable while someone is home — next iteration disarms.
    app_state.is_app_enabled = True
    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)
    mock_blink.disarm_cameras.assert_awaited_once_with(["Backyard"])
    assert app_state.commanded_camera_states["Backyard"] is False


@pytest.mark.asyncio
async def test_not_connected_calls_connect(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    mock_blink.is_connected = False
    mock_blink.connect.return_value = ConnectResult.OK

    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    mock_blink.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_needs_2fa_sets_pending_no_arm_disarm(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    mock_blink.is_connected = False
    mock_blink.connect.return_value = ConnectResult.NEEDS_2FA

    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    assert app_state.is_2fa_pending is True
    mock_blink.arm_cameras.assert_not_awaited()
    mock_blink.disarm_cameras.assert_not_awaited()


@pytest.mark.asyncio
async def test_connect_failed_retries_next_iteration_app_stays_enabled(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    mock_blink.is_connected = False
    mock_blink.connect.return_value = ConnectResult.FAILED

    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    assert app_state.is_app_enabled is True  # not permanently disabled
    mock_blink.arm_cameras.assert_not_awaited()
    assert ctx.connect_failure_count == 1


@pytest.mark.asyncio
async def test_connect_failed_backs_off_before_next_attempt(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    """After a connection failure, a subsequent iteration within the
    backoff window must not call connect() again."""
    mock_blink.is_connected = False
    mock_blink.connect.return_value = ConnectResult.FAILED

    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)
    assert mock_blink.connect.await_count == 1

    # Immediately run another iteration — still within backoff window.
    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)
    assert mock_blink.connect.await_count == 1  # not retried yet


@pytest.mark.asyncio
async def test_connect_success_resets_backoff_state(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    mock_blink.is_connected = False
    ctx.connect_failure_count = 3
    ctx.next_connect_attempt_time = 0.0
    mock_blink.connect.return_value = ConnectResult.OK

    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    assert ctx.connect_failure_count == 0
    assert ctx.next_connect_attempt_time == 0.0


@pytest.mark.asyncio
async def test_2fa_pending_with_code_calls_submit(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    app_state.is_2fa_pending = True
    app_state.received_2fa_code = "123456"

    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    mock_blink.submit_2fa_code.assert_awaited_once_with("123456")
    assert app_state.received_2fa_code is None
    assert app_state.is_2fa_pending is False


@pytest.mark.asyncio
async def test_2fa_pending_without_code_does_not_call_connect(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    """While a 2FA challenge is outstanding and no code has arrived yet,
    the iteration must return immediately rather than falling through
    to connect() — which would replace the Blink object and discard the
    pending challenge state."""
    app_state.is_2fa_pending = True
    app_state.received_2fa_code = None
    mock_blink.is_connected = False

    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    mock_blink.connect.assert_not_awaited()
    mock_blink.submit_2fa_code.assert_not_awaited()
    assert app_state.is_2fa_pending is True


@pytest.mark.asyncio
async def test_2fa_pending_without_code_across_several_iterations(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    """Several iterations with no code submitted must never call
    connect() — the challenge state must survive indefinitely until a
    code arrives."""
    app_state.is_2fa_pending = True
    mock_blink.is_connected = False

    for _ in range(5):
        await _run(
            app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
        )

    mock_blink.connect.assert_not_awaited()
    assert app_state.is_2fa_pending is True


@pytest.mark.asyncio
async def test_motion_alerts_disabled_does_not_poll(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    app_config.motion_alerts_enabled = False

    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    mock_blink.get_new_motion_events.assert_not_awaited()


@pytest.mark.asyncio
async def test_motion_alerts_scoped_to_controlled_cameras(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    """Motion polling must be scoped to the controlled-camera allowlist,
    not every account camera."""
    app_config.motion_alerts_enabled = True
    app_config.controlled_cameras = ["Backyard"]

    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    mock_blink.get_new_motion_events.assert_awaited_once()
    _, kwargs = mock_blink.get_new_motion_events.call_args
    assert kwargs["camera_names"] == ["Backyard"]


@pytest.mark.asyncio
async def test_motion_alert_fires_send_video_after_priming(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    app_config.motion_alerts_enabled = True
    event = MotionEvent(
        camera_name="Backyard", clip_time="2024-01-01T00:00:00", clip_bytes=b"v"
    )

    # First iteration: priming call — no clips yet, nothing to send.
    mock_blink.get_new_motion_events.return_value = []
    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)
    mock_bot.send_video.assert_not_awaited()

    # Second iteration: new motion clip appears -> alert sent.
    mock_blink.get_new_motion_events.return_value = [event]
    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    mock_bot.send_video.assert_awaited_once()
    args, kwargs = mock_bot.send_video.call_args
    assert "Backyard" in args[0]
    assert args[1] == b"v"


# --- Health / state-transition logging ---


@pytest.mark.asyncio
async def test_iteration_records_time_of_last_iteration(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    """Every iteration that actually runs (app enabled) must stamp
    state.time_of_last_iteration, so /cambot status can distinguish
    a live-but-wedged process from one still making progress."""
    assert app_state.time_of_last_iteration is None

    before = time.time()
    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)
    after = time.time()

    assert app_state.time_of_last_iteration is not None
    assert before <= app_state.time_of_last_iteration <= after


@pytest.mark.asyncio
async def test_app_disabled_does_not_record_iteration_time(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
) -> None:
    """A disabled app returns before doing anything — including before
    stamping the health signal, since no real iteration ran."""
    app_state.is_app_enabled = False

    await _run(app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx)

    assert app_state.time_of_last_iteration is None


@pytest.mark.asyncio
async def test_presence_transition_logged_once_per_change(
    app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx, caplog
) -> None:
    """A presence transition is logged; an unchanged reading on the next
    iteration must not log again."""
    mock_monitor.check_all.return_value = Presence.AWAY

    with caplog.at_level("INFO", logger="blink_camera_auto_arm"):
        await _run(
            app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
        )
        transition_logs = [
            r for r in caplog.records if "Presence transition" in r.message
        ]
        assert len(transition_logs) == 1
        assert "startup -> away" in transition_logs[0].message

        caplog.clear()
        # Same presence again — no new transition log.
        await _run(
            app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
        )
        transition_logs = [
            r for r in caplog.records if "Presence transition" in r.message
        ]
        assert transition_logs == []

        caplog.clear()
        # Presence changes — logs exactly one new transition.
        mock_monitor.check_all.return_value = Presence.HOME
        await _run(
            app_config, app_state, mock_blink, mock_monitor, mock_bot, ctx
        )
        transition_logs = [
            r for r in caplog.records if "Presence transition" in r.message
        ]
        assert len(transition_logs) == 1
        assert "away -> home" in transition_logs[0].message


def test_configure_logging_silences_httpx_and_httpcore() -> None:
    """_configure_logging() must raise httpx/httpcore loggers to WARNING
    so python-telegram-bot's routine getUpdates polling doesn't spam the
    log at INFO every ~10s, without touching other loggers' levels."""
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    other_logger = logging.getLogger("blink_camera_auto_arm")
    httpx_logger.setLevel(logging.NOTSET)
    httpcore_logger.setLevel(logging.NOTSET)
    original_other_level = other_logger.level
    try:
        _configure_logging()
        assert httpx_logger.level == logging.WARNING
        assert httpcore_logger.level == logging.WARNING
        assert other_logger.level == original_other_level
    finally:
        httpx_logger.setLevel(logging.NOTSET)
        httpcore_logger.setLevel(logging.NOTSET)
        other_logger.setLevel(original_other_level)
