"""Tests for telegram_bot.py — TelegramBot command router + handlers."""

import time
from unittest.mock import AsyncMock, MagicMock

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
    svc.snapshot = AsyncMock(return_value=None)
    svc.get_latest_clip = AsyncMock(return_value=None)
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
    than the more generic (and less actionable) 'not connected' —
    codereview.md L-3 observability improvement."""
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
