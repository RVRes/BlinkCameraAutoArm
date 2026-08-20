"""Tests for telegram_bot.py — TelegramBot command router + handlers."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blink_service import CameraInfo
from config import AppConfig, Config
from state import AppState
from telegram_bot import TelegramBot

CHAT_ID = 100
ALLOWED_USER_ID = 999


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig(
        blink_username="test@example.com",
        blink_password="password",
        telegram_bot_token="token",
        telegram_chat_id=CHAT_ID,
        telegram_allowed_user_id=ALLOWED_USER_ID,
        monitored_ips=["192.168.0.1"],
        controlled_cameras=[],
        absence_checks=3,
        ping_interval_seconds=60,
        motion_alerts_enabled=False,
    )


@pytest.fixture
def app_state() -> AppState:
    return AppState()


@pytest.fixture
def mock_config() -> MagicMock:
    config = MagicMock(spec=Config)
    config.save = MagicMock()
    return config


@pytest.fixture
def mock_blink_service() -> MagicMock:
    svc = MagicMock()
    svc.is_connected = True
    svc.list_all_cameras = MagicMock(return_value=[])
    svc.has_camera = MagicMock(return_value=True)
    svc.snapshot = AsyncMock(return_value=None)
    svc.get_latest_clip = AsyncMock(return_value=None)
    svc.refresh = AsyncMock()
    svc.arm_cameras = AsyncMock(
        side_effect=lambda names: dict.fromkeys(names, True)
    )
    svc.disarm_cameras = AsyncMock(
        side_effect=lambda names: dict.fromkeys(names, True)
    )
    return svc


@pytest.fixture
def mock_presence_monitor() -> MagicMock:
    mon = MagicMock()
    mon.add_ip = MagicMock()
    mon.remove_ip = MagicMock()
    return mon


@pytest.fixture
def bot(
    app_config: AppConfig,
    mock_config: MagicMock,
    app_state: AppState,
    mock_blink_service: MagicMock,
    mock_presence_monitor: MagicMock,
) -> TelegramBot:
    return TelegramBot(
        token=app_config.telegram_bot_token,
        chat_id=app_config.telegram_chat_id,
        allowed_user_id=app_config.telegram_allowed_user_id,
        config=mock_config,
        app_cfg=app_config,
        state=app_state,
        blink=mock_blink_service,
        monitor=mock_presence_monitor,
    )


def _make_update(chat_id: int, user_id: int) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.id = user_id
    update.effective_message.reply_text = AsyncMock()
    return update


def _make_context(args: list[str]) -> MagicMock:
    context = MagicMock()
    context.args = args
    context.bot.send_message = AsyncMock()
    context.bot.send_photo = AsyncMock()
    context.bot.send_video = AsyncMock()
    return context


async def _send_command(bot: TelegramBot, args: list[str]) -> MagicMock:
    update = _make_update(CHAT_ID, ALLOWED_USER_ID)
    context = _make_context(args)
    await bot.handle_command(update, context)
    return context


# --- Authorization gate ---


@pytest.mark.asyncio
async def test_authorization_wrong_chat_id_drops_command(
    bot: TelegramBot,
) -> None:
    update = _make_update(chat_id=0, user_id=ALLOWED_USER_ID)
    context = _make_context(["status"])

    await bot.handle_command(update, context)

    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorization_wrong_user_id_drops_command(
    bot: TelegramBot,
) -> None:
    update = _make_update(chat_id=CHAT_ID, user_id=0)
    context = _make_context(["status"])

    await bot.handle_command(update, context)

    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorization_correct_chat_and_user_invokes_handler(
    bot: TelegramBot,
) -> None:
    context = await _send_command(bot, ["status"])
    context.bot.send_message.assert_awaited()


# --- help ---


@pytest.mark.asyncio
async def test_help_lists_all_commands(bot: TelegramBot) -> None:
    context = await _send_command(bot, ["help"])
    message = context.bot.send_message.call_args.kwargs["text"]
    for command in [
        "help",
        "status",
        "enable",
        "disable",
        "cameras",
        "ips",
        "2fa",
        "snapshot",
        "clip",
        "alerts",
    ]:
        assert command in message


@pytest.mark.asyncio
async def test_no_args_shows_help(bot: TelegramBot) -> None:
    context = await _send_command(bot, [])
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "help" in message


# --- status ---


@pytest.mark.asyncio
async def test_status_reports_enabled_disabled_and_ip_status(
    bot: TelegramBot, app_state: AppState
) -> None:
    app_state.is_app_enabled = True
    app_state.ip_ping_status = {"192.168.0.1": True}
    context = await _send_command(bot, ["status"])
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "enabled" in message
    assert "192.168.0.1" in message


@pytest.mark.asyncio
async def test_status_when_not_connected_skips_camera_fields(
    bot: TelegramBot, mock_blink_service: MagicMock
) -> None:
    mock_blink_service.is_connected = False
    context = await _send_command(bot, ["status"])
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "not connected" in message.lower()


@pytest.mark.asyncio
async def test_status_shows_unknown_for_ip_not_yet_pinged(
    bot: TelegramBot, app_config: AppConfig, app_state: AppState
) -> None:
    """A monitored IP with no ping result yet shows 'unknown' in status,
    matching 'ips list' behavior, instead of being silently omitted."""
    app_config.monitored_ips = ["192.168.0.1"]
    app_state.ip_ping_status = {}
    context = await _send_command(bot, ["status"])
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "192.168.0.1" in message
    assert "unknown" in message.lower()


@pytest.mark.asyncio
async def test_status_shows_2fa_pending_instead_of_not_connected(
    bot: TelegramBot, app_state: AppState, mock_blink_service: MagicMock
) -> None:
    """When a 2FA challenge is outstanding, status must say so rather
    than the more generic (and less actionable) 'not connected'."""
    app_state.is_2fa_pending = True
    mock_blink_service.is_connected = False
    context = await _send_command(bot, ["status"])
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "2fa pending" in message.lower()


@pytest.mark.asyncio
async def test_status_shows_last_iteration_never_by_default(
    bot: TelegramBot,
) -> None:
    context = await _send_command(bot, ["status"])
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "last main-loop iteration" in message.lower()
    assert "never" in message.lower()


@pytest.mark.asyncio
async def test_status_shows_last_iteration_elapsed_time(
    bot: TelegramBot, app_state: AppState
) -> None:
    app_state.time_of_last_iteration = time.time()
    context = await _send_command(bot, ["status"])
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "ago" in message.lower()


@pytest.mark.asyncio
async def test_status_camera_section_uses_multiline_layout(
    bot: TelegramBot, app_config: AppConfig, mock_blink_service: MagicMock
) -> None:
    """Each camera's status must be rendered as a blank line followed by
    the camera name, then one indented attribute per line — not the old
    single pipe-delimited line — so the section is readable on mobile
    Telegram clients."""
    app_config.controlled_cameras = ["Front Door"]
    mock_blink_service.list_all_cameras.return_value = [
        CameraInfo(
            name="Front Door",
            camera_id="1",
            network_id="10",
            product_type="catalina",
            online=True,
            armed=True,
            battery="ok",
        )
    ]
    context = await _send_command(bot, ["status"])
    message = context.bot.send_message.call_args.kwargs["text"]
    expected_block = (
        "\n  Front Door\n"
        "    armed: yes\n"
        "    online: yes\n"
        "    battery: ok\n"
        "    controlled: yes"
    )
    assert expected_block in message
    assert "|" not in message


# --- enable / disable ---


@pytest.mark.asyncio
async def test_enable_sets_state_enabled(
    bot: TelegramBot, app_state: AppState
) -> None:
    app_state.is_app_enabled = False
    await _send_command(bot, ["enable"])
    assert app_state.is_app_enabled is True


@pytest.mark.asyncio
async def test_disable_sets_state_disabled(
    bot: TelegramBot, app_state: AppState
) -> None:
    app_state.is_app_enabled = True
    await _send_command(bot, ["disable"])
    assert app_state.is_app_enabled is False


# --- manual arm / disarm ---


@pytest.mark.asyncio
async def test_arm_no_name_arms_all_controlled_cameras(
    bot: TelegramBot,
    app_config: AppConfig,
    app_state: AppState,
    mock_blink_service: MagicMock,
) -> None:
    app_config.controlled_cameras = ["Front Door", "Backyard"]
    context = await _send_command(bot, ["arm"])
    mock_blink_service.arm_cameras.assert_awaited_once_with(
        ["Front Door", "Backyard"]
    )
    assert app_state.commanded_camera_states == {
        "Front Door": True,
        "Backyard": True,
    }
    assert app_state.time_of_last_arm_change is not None
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "Front Door" in message and "Backyard" in message


@pytest.mark.asyncio
async def test_arm_with_name_arms_only_that_camera(
    bot: TelegramBot,
    app_config: AppConfig,
    app_state: AppState,
    mock_blink_service: MagicMock,
) -> None:
    app_config.controlled_cameras = ["Front Door", "Backyard"]
    await _send_command(bot, ["arm", "Backyard"])
    mock_blink_service.arm_cameras.assert_awaited_once_with(["Backyard"])
    assert app_state.commanded_camera_states == {"Backyard": True}


@pytest.mark.asyncio
async def test_arm_unknown_camera_name_rejected(
    bot: TelegramBot,
    app_config: AppConfig,
    mock_blink_service: MagicMock,
) -> None:
    app_config.controlled_cameras = ["Front Door"]
    context = await _send_command(bot, ["arm", "Nonexistent"])
    mock_blink_service.arm_cameras.assert_not_awaited()
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "not in the auto-arm list" in message.lower()


@pytest.mark.asyncio
async def test_disarm_no_name_disarms_all_controlled_cameras(
    bot: TelegramBot,
    app_config: AppConfig,
    app_state: AppState,
    mock_blink_service: MagicMock,
) -> None:
    app_config.controlled_cameras = ["Front Door", "Backyard"]
    context = await _send_command(bot, ["disarm"])
    mock_blink_service.disarm_cameras.assert_awaited_once_with(
        ["Front Door", "Backyard"]
    )
    assert app_state.commanded_camera_states == {
        "Front Door": False,
        "Backyard": False,
    }
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "Front Door" in message and "Backyard" in message


@pytest.mark.asyncio
async def test_disarm_with_name_disarms_only_that_camera(
    bot: TelegramBot,
    app_config: AppConfig,
    app_state: AppState,
    mock_blink_service: MagicMock,
) -> None:
    app_config.controlled_cameras = ["Front Door", "Backyard"]
    await _send_command(bot, ["disarm", "Front Door"])
    mock_blink_service.disarm_cameras.assert_awaited_once_with(["Front Door"])
    assert app_state.commanded_camera_states == {"Front Door": False}


@pytest.mark.asyncio
async def test_arm_no_controlled_cameras_configured(
    bot: TelegramBot, mock_blink_service: MagicMock
) -> None:
    context = await _send_command(bot, ["arm"])
    mock_blink_service.arm_cameras.assert_not_awaited()
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "no controlled cameras" in message.lower()


@pytest.mark.asyncio
async def test_arm_not_gated_on_is_app_enabled(
    bot: TelegramBot,
    app_config: AppConfig,
    app_state: AppState,
    mock_blink_service: MagicMock,
) -> None:
    """/cambot disable followed by /cambot arm must still work — arm is
    not gated on is_app_enabled."""
    app_config.controlled_cameras = ["Front Door"]
    await _send_command(bot, ["disable"])
    assert app_state.is_app_enabled is False
    await _send_command(bot, ["arm"])
    mock_blink_service.arm_cameras.assert_awaited_once_with(["Front Door"])
    assert app_state.commanded_camera_states == {"Front Door": True}


@pytest.mark.asyncio
async def test_arm_when_not_connected(
    bot: TelegramBot,
    app_config: AppConfig,
    mock_blink_service: MagicMock,
) -> None:
    app_config.controlled_cameras = ["Front Door"]
    mock_blink_service.is_connected = False
    context = await _send_command(bot, ["arm"])
    mock_blink_service.arm_cameras.assert_not_awaited()
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "not connected" in message.lower()


@pytest.mark.asyncio
async def test_arm_camera_not_found_in_account_reports_skip(
    bot: TelegramBot,
    app_config: AppConfig,
    app_state: AppState,
    mock_blink_service: MagicMock,
) -> None:
    """A controlled-camera name that Blink no longer recognizes (e.g.
    renamed/deleted) must be reported as skipped, not silently
    succeeded, and must not update commanded_camera_states."""
    app_config.controlled_cameras = ["Stale Camera"]
    mock_blink_service.arm_cameras = AsyncMock(
        return_value={"Stale Camera": False}
    )
    context = await _send_command(bot, ["arm"])
    assert app_state.commanded_camera_states == {}
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "stale camera" in message.lower()
    assert "skipped" in message.lower()


# --- cameras list/add/remove ---


@pytest.mark.asyncio
async def test_cameras_list_lists_all_account_cameras(
    bot: TelegramBot, mock_blink_service: MagicMock
) -> None:
    mock_blink_service.list_all_cameras.return_value = [
        CameraInfo(
            name="Backyard",
            camera_id="1",
            network_id="10",
            product_type="catalina",
            online=True,
            armed=True,
            battery="ok",
        )
    ]
    context = await _send_command(bot, ["cameras", "list"])
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "Backyard" in message


@pytest.mark.asyncio
async def test_cameras_list_when_not_connected(
    bot: TelegramBot, mock_blink_service: MagicMock
) -> None:
    mock_blink_service.is_connected = False
    context = await _send_command(bot, ["cameras", "list"])
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "not connected" in message.lower()


@pytest.mark.asyncio
async def test_cameras_add_valid_name_adds_and_saves(
    bot: TelegramBot,
    app_config: AppConfig,
    mock_blink_service: MagicMock,
    mock_config: MagicMock,
) -> None:
    mock_blink_service.list_all_cameras.return_value = [
        CameraInfo(
            name="Backyard",
            camera_id="1",
            network_id="10",
            product_type="catalina",
            online=True,
            armed=True,
            battery="ok",
        )
    ]
    await _send_command(bot, ["cameras", "add", "Backyard"])
    assert "Backyard" in app_config.controlled_cameras
    mock_config.save.assert_called_once_with(app_config)


@pytest.mark.asyncio
async def test_cameras_add_invalid_name_does_not_mutate_config(
    bot: TelegramBot,
    app_config: AppConfig,
    mock_blink_service: MagicMock,
    mock_config: MagicMock,
) -> None:
    mock_blink_service.list_all_cameras.return_value = []
    context = await _send_command(bot, ["cameras", "add", "Ghost"])
    assert "Ghost" not in app_config.controlled_cameras
    mock_config.save.assert_not_called()
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "error" in message.lower() or "not found" in message.lower()


@pytest.mark.asyncio
async def test_cameras_add_when_not_connected_rejects(
    bot: TelegramBot,
    app_config: AppConfig,
    mock_blink_service: MagicMock,
    mock_config: MagicMock,
) -> None:
    mock_blink_service.is_connected = False
    context = await _send_command(bot, ["cameras", "add", "Backyard"])
    assert app_config.controlled_cameras == []
    mock_config.save.assert_not_called()
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "not connected" in message.lower()


@pytest.mark.asyncio
async def test_cameras_remove_removes_and_saves(
    bot: TelegramBot, app_config: AppConfig, mock_config: MagicMock
) -> None:
    app_config.controlled_cameras = ["Backyard"]
    await _send_command(bot, ["cameras", "remove", "Backyard"])
    assert "Backyard" not in app_config.controlled_cameras
    mock_config.save.assert_called_once_with(app_config)


@pytest.mark.asyncio
async def test_cameras_add_missing_name_shows_usage(bot: TelegramBot) -> None:
    context = await _send_command(bot, ["cameras", "add"])
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "usage" in message.lower()


# --- cameras refresh + stale-camera reconciliation (item 9) ---


@pytest.mark.asyncio
async def test_cameras_refresh_forces_live_refresh(
    bot: TelegramBot, mock_blink_service: MagicMock
) -> None:
    """cameras refresh must call blink.refresh() directly, independent
    of the main loop's periodic cadence."""
    await _send_command(bot, ["cameras", "refresh"])
    mock_blink_service.refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_cameras_refresh_when_not_connected(
    bot: TelegramBot, mock_blink_service: MagicMock
) -> None:
    mock_blink_service.is_connected = False
    context = await _send_command(bot, ["cameras", "refresh"])
    mock_blink_service.refresh.assert_not_awaited()
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "not connected" in message.lower()


@pytest.mark.asyncio
async def test_cameras_refresh_reports_no_changes_when_nothing_stale(
    bot: TelegramBot, app_config: AppConfig, mock_blink_service: MagicMock
) -> None:
    app_config.controlled_cameras = ["Backyard"]
    mock_blink_service.list_all_cameras.return_value = [
        CameraInfo(
            name="Backyard",
            camera_id="1",
            network_id="10",
            product_type="catalina",
            online=True,
            armed=True,
            battery="ok",
        )
    ]
    context = await _send_command(bot, ["cameras", "refresh"])
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "no changes" in message.lower()
    assert app_config.controlled_cameras == ["Backyard"]


@pytest.mark.asyncio
async def test_cameras_refresh_removes_stale_camera_and_reports(
    bot: TelegramBot,
    app_config: AppConfig,
    app_state: AppState,
    mock_blink_service: MagicMock,
    mock_config: MagicMock,
) -> None:
    """A camera renamed/deleted on the Blink side must be removed from
    controlled_cameras (persisted) after a forced refresh, with the
    stale name(s) reported in the command's reply."""
    app_config.controlled_cameras = ["Old Name", "Backyard"]
    app_state.commanded_camera_states = {"Old Name": True, "Backyard": False}
    app_state.camera_armed_status = {"Old Name": True, "Backyard": False}
    mock_blink_service.list_all_cameras.return_value = [
        CameraInfo(
            name="Backyard",
            camera_id="1",
            network_id="10",
            product_type="catalina",
            online=True,
            armed=False,
            battery="ok",
        )
    ]

    context = await _send_command(bot, ["cameras", "refresh"])

    assert app_config.controlled_cameras == ["Backyard"]
    assert "Old Name" not in app_state.commanded_camera_states
    assert "Old Name" not in app_state.camera_armed_status
    mock_config.save.assert_called_once_with(app_config)
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "old name" in message.lower()
    assert "removed" in message.lower()


@pytest.mark.asyncio
async def test_cameras_refresh_failure_reports_error(
    bot: TelegramBot, mock_blink_service: MagicMock
) -> None:
    mock_blink_service.refresh.side_effect = RuntimeError("boom")
    context = await _send_command(bot, ["cameras", "refresh"])
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "failed" in message.lower()


def test_reconcile_stale_cameras_when_not_connected_returns_empty(
    bot: TelegramBot, app_config: AppConfig, mock_blink_service: MagicMock
) -> None:
    """Reconciliation must be a safe no-op when not connected — e.g.
    called defensively from the main loop after a failed refresh."""
    app_config.controlled_cameras = ["Backyard"]
    mock_blink_service.is_connected = False

    stale = bot.reconcile_stale_cameras()

    assert stale == []
    assert app_config.controlled_cameras == ["Backyard"]


def test_reconcile_stale_cameras_no_controlled_cameras_returns_empty(
    bot: TelegramBot, mock_blink_service: MagicMock
) -> None:
    stale = bot.reconcile_stale_cameras()
    assert stale == []


# --- ips list/add/remove ---


@pytest.mark.asyncio
async def test_ips_list_shows_status(
    bot: TelegramBot, app_state: AppState
) -> None:
    app_state.ip_ping_status = {"192.168.0.1": True}
    context = await _send_command(bot, ["ips", "list"])
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "192.168.0.1" in message


@pytest.mark.asyncio
async def test_ips_add_valid_ip_adds_saves_and_calls_monitor(
    bot: TelegramBot,
    app_config: AppConfig,
    mock_config: MagicMock,
    mock_presence_monitor: MagicMock,
) -> None:
    await _send_command(bot, ["ips", "add", "192.168.0.10"])
    assert "192.168.0.10" in app_config.monitored_ips
    mock_config.save.assert_called_once_with(app_config)
    mock_presence_monitor.add_ip.assert_called_once_with("192.168.0.10")


@pytest.mark.asyncio
async def test_ips_add_invalid_format_rejected(
    bot: TelegramBot,
    app_config: AppConfig,
    mock_config: MagicMock,
    mock_presence_monitor: MagicMock,
) -> None:
    original = list(app_config.monitored_ips)
    context = await _send_command(bot, ["ips", "add", "999.999.0.1"])
    assert app_config.monitored_ips == original
    mock_config.save.assert_not_called()
    mock_presence_monitor.add_ip.assert_not_called()
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "invalid" in message.lower()


@pytest.mark.asyncio
async def test_ips_remove_removes_saves_and_calls_monitor(
    bot: TelegramBot,
    app_config: AppConfig,
    mock_config: MagicMock,
    mock_presence_monitor: MagicMock,
) -> None:
    app_config.monitored_ips = ["192.168.0.10"]
    await _send_command(bot, ["ips", "remove", "192.168.0.10"])
    assert "192.168.0.10" not in app_config.monitored_ips
    mock_config.save.assert_called_once_with(app_config)
    mock_presence_monitor.remove_ip.assert_called_once_with("192.168.0.10")


# --- 2fa ---


@pytest.mark.asyncio
async def test_2fa_while_pending_sets_received_code(
    bot: TelegramBot, app_state: AppState
) -> None:
    app_state.is_2fa_pending = True
    await _send_command(bot, ["2fa", "123456"])
    assert app_state.received_2fa_code == "123456"


@pytest.mark.asyncio
async def test_2fa_while_not_pending_informs_user(
    bot: TelegramBot, app_state: AppState
) -> None:
    app_state.is_2fa_pending = False
    context = await _send_command(bot, ["2fa", "123456"])
    assert app_state.received_2fa_code is None
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "no 2fa" in message.lower() or "not expect" in message.lower()


# --- snapshot / clip ---


@pytest.mark.asyncio
async def test_snapshot_sends_photo(
    bot: TelegramBot, mock_blink_service: MagicMock
) -> None:
    mock_blink_service.snapshot.return_value = b"jpeg"
    context = await _send_command(bot, ["snapshot", "Backyard"])
    mock_blink_service.snapshot.assert_awaited_once_with("Backyard")
    context.bot.send_photo.assert_awaited_once()


@pytest.mark.asyncio
async def test_snapshot_none_sends_error(
    bot: TelegramBot, mock_blink_service: MagicMock
) -> None:
    mock_blink_service.snapshot.return_value = None
    context = await _send_command(bot, ["snapshot", "Backyard"])
    context.bot.send_photo.assert_not_awaited()
    context.bot.send_message.assert_awaited()
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "could not get snapshot" in message.lower()


@pytest.mark.asyncio
async def test_snapshot_unknown_camera_name_sends_not_found(
    bot: TelegramBot, mock_blink_service: MagicMock
) -> None:
    """A nonexistent camera name must get a distinct 'not found' message,
    not the generic 'could not get snapshot' wording used when a real
    camera simply has no snapshot available yet."""
    mock_blink_service.has_camera.return_value = False
    context = await _send_command(bot, ["snapshot", "Nonexistent"])
    mock_blink_service.snapshot.assert_not_awaited()
    context.bot.send_photo.assert_not_awaited()
    message = context.bot.send_message.call_args.kwargs["text"]
    assert message == "No camera named 'Nonexistent' found."


@pytest.mark.asyncio
async def test_snapshot_when_not_connected(
    bot: TelegramBot, mock_blink_service: MagicMock
) -> None:
    mock_blink_service.is_connected = False
    context = await _send_command(bot, ["snapshot", "Backyard"])
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "not connected" in message.lower()


@pytest.mark.asyncio
async def test_clip_sends_video(
    bot: TelegramBot, mock_blink_service: MagicMock
) -> None:
    mock_blink_service.get_latest_clip.return_value = b"video"
    context = await _send_command(bot, ["clip", "Backyard"])
    mock_blink_service.get_latest_clip.assert_awaited_once_with("Backyard")
    context.bot.send_video.assert_awaited_once()


@pytest.mark.asyncio
async def test_clip_none_sends_error(
    bot: TelegramBot, mock_blink_service: MagicMock
) -> None:
    mock_blink_service.get_latest_clip.return_value = None
    context = await _send_command(bot, ["clip", "Backyard"])
    context.bot.send_video.assert_not_awaited()
    context.bot.send_message.assert_awaited()
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "no clip available" in message.lower()


@pytest.mark.asyncio
async def test_clip_unknown_camera_name_sends_not_found(
    bot: TelegramBot, mock_blink_service: MagicMock
) -> None:
    """A nonexistent camera name must get a distinct 'not found' message,
    not the generic 'no clip available' wording used when a real camera
    simply has no clip available yet."""
    mock_blink_service.has_camera.return_value = False
    context = await _send_command(bot, ["clip", "Nonexistent"])
    mock_blink_service.get_latest_clip.assert_not_awaited()
    context.bot.send_video.assert_not_awaited()
    message = context.bot.send_message.call_args.kwargs["text"]
    assert message == "No camera named 'Nonexistent' found."


# --- alerts on/off ---


@pytest.mark.asyncio
async def test_alerts_on_enables_and_saves(
    bot: TelegramBot, app_config: AppConfig, mock_config: MagicMock
) -> None:
    app_config.motion_alerts_enabled = False
    await _send_command(bot, ["alerts", "on"])
    assert app_config.motion_alerts_enabled is True
    mock_config.save.assert_called_once_with(app_config)


@pytest.mark.asyncio
async def test_alerts_off_disables_and_saves(
    bot: TelegramBot, app_config: AppConfig, mock_config: MagicMock
) -> None:
    app_config.motion_alerts_enabled = True
    await _send_command(bot, ["alerts", "off"])
    assert app_config.motion_alerts_enabled is False
    mock_config.save.assert_called_once_with(app_config)


# --- unknown subcommand ---


@pytest.mark.asyncio
async def test_unknown_subcommand_sends_error_and_help(
    bot: TelegramBot,
) -> None:
    context = await _send_command(bot, ["bogus"])
    message = context.bot.send_message.call_args.kwargs["text"]
    assert "unknown" in message.lower() or "help" in message.lower()


# --- start()/shutdown() lifecycle ---
#
# Regression coverage for a real bug: PTB's start_polling() only blocks
# until its background polling task is confirmed running, then returns
# immediately — it does NOT block for the lifetime of polling. Before
# start() awaited an internal stop event, its coroutine (and thus the
# asyncio.Task wrapping it in main()) completed within a fraction of a
# second of every launch. main()'s asyncio.wait(..., return_when=
# FIRST_COMPLETED) then misread that early, successful completion as
# "the bot task is done" — indistinguishable from a crash or a shutdown
# request — and tore down the whole app almost immediately after every
# start. On the router, this meant the cron watchdog would relaunch the
# app roughly every minute, forever, instead of it running continuously.
#
# These tests assert the actual contract main() depends on: start()
# must stay running (not just "not raise") until shutdown() is called
# or the task is cancelled — exactly like run_main_loop().


def _make_mock_application() -> MagicMock:
    application = MagicMock()
    application.initialize = AsyncMock()
    application.start = AsyncMock()
    application.stop = AsyncMock()
    application.shutdown = AsyncMock()
    application.updater = MagicMock()
    application.updater.start_polling = AsyncMock()
    application.updater.stop = AsyncMock()
    application.bot.send_message = AsyncMock()
    return application


@pytest.mark.asyncio
async def test_start_does_not_return_after_polling_begins(
    bot: TelegramBot,
) -> None:
    """start() must still be running well after start_polling() has
    returned — it must not complete just because setup finished."""
    mock_application = _make_mock_application()
    with patch("telegram_bot.Application.builder") as mock_builder:
        mock_builder.return_value.token.return_value.build.return_value = (
            mock_application
        )
        task = asyncio.create_task(bot.start())
        try:
            # Give start() every chance to (incorrectly) return on its
            # own after setup completes.
            await asyncio.sleep(0.05)
            assert not task.done(), (
                "start() returned after polling was set up instead of "
                "staying alive for the app's lifetime — this is the "
                "exact bug that caused the app to exit ~1 second after "
                "every launch."
            )
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_shutdown_lets_a_running_start_task_return(
    bot: TelegramBot,
) -> None:
    """shutdown() must let an in-flight start() task finish on its own
    (via the stop event) rather than requiring external cancellation."""
    mock_application = _make_mock_application()
    with patch("telegram_bot.Application.builder") as mock_builder:
        mock_builder.return_value.token.return_value.build.return_value = (
            mock_application
        )
        task = asyncio.create_task(bot.start())
        await asyncio.sleep(0.05)
        assert not task.done()

        await bot.shutdown()
        await asyncio.wait_for(task, timeout=1)
        assert task.done()
        assert task.exception() is None
