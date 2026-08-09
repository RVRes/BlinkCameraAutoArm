"""Tests for blink_service.py — BlinkService wrapper around blinkpy."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blink_service import (
    BlinkService,
    BlinkTimeoutError,
    CameraInfo,
    ConnectResult,
    MotionEvent,
)


class _FakeAuth:
    def __init__(self, login_data=None, no_prompt=False, session=None):
        self.login_data = login_data or {}
        self.no_prompt = no_prompt
        self.login_attributes = {**self.login_data, "refresh_token": "rt"}


def _make_blink_mock(start_result=True, cameras=None):
    blink = MagicMock()
    blink.start = AsyncMock(return_value=start_result)
    blink.refresh = AsyncMock(return_value=True)
    blink.send_2fa_code = AsyncMock(return_value=True)
    blink.save = AsyncMock()
    blink.cameras = cameras if cameras is not None else {}
    blink.auth = MagicMock()
    blink.auth.login_attributes = {"token": "abc", "refresh_token": "rt"}
    return blink


@pytest.fixture(autouse=True)
def _no_real_files(tmp_path, monkeypatch):
    """Redirect the credentials file into a tmp dir for every test."""
    monkeypatch.chdir(tmp_path)


@pytest.mark.asyncio
async def test_connect_with_valid_saved_credentials_returns_ok() -> None:
    service = BlinkService("user@example.com", "pw")
    blink = _make_blink_mock(start_result=True)

    with (
        patch(
            "blink_service.json_load",
            new=AsyncMock(return_value={"token": "x"}),
        ),
        patch("blink_service.os.path.exists", return_value=True),
        patch("blink_service.Blink", return_value=blink),
        patch("blink_service.Auth", _FakeAuth),
    ):
        result = await service.connect()

    assert result == ConnectResult.OK
    assert service.is_connected is False  # no cameras populated in mock
    blink.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_with_no_credentials_file_merges_username_password() -> (
    None
):
    service = BlinkService("user@example.com", "pw")
    blink = _make_blink_mock(start_result=True)
    captured = {}

    def fake_auth_init(login_data=None, no_prompt=False, session=None):
        captured["login_data"] = login_data
        return _FakeAuth(login_data, no_prompt, session)

    with (
        patch("blink_service.os.path.exists", return_value=False),
        patch("blink_service.Blink", return_value=blink),
        patch("blink_service.Auth", side_effect=fake_auth_init),
    ):
        result = await service.connect()

    assert result == ConnectResult.OK
    assert captured["login_data"]["username"] == "user@example.com"
    assert captured["login_data"]["password"] == "pw"
    blink.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_merges_saved_credentials_with_username_password() -> (
    None
):
    service = BlinkService("user@example.com", "pw")
    blink = _make_blink_mock(start_result=True)
    captured = {}

    def fake_auth_init(login_data=None, no_prompt=False, session=None):
        captured["login_data"] = login_data
        return _FakeAuth(login_data, no_prompt, session)

    saved = {"refresh_token": "rt", "hardware_id": "hw-1"}
    with (
        patch("blink_service.json_load", new=AsyncMock(return_value=saved)),
        patch("blink_service.os.path.exists", return_value=True),
        patch("blink_service.Blink", return_value=blink),
        patch("blink_service.Auth", side_effect=fake_auth_init),
    ):
        await service.connect()

    assert captured["login_data"]["username"] == "user@example.com"
    assert captured["login_data"]["password"] == "pw"
    assert captured["login_data"]["refresh_token"] == "rt"
    assert captured["login_data"]["hardware_id"] == "hw-1"


@pytest.mark.asyncio
async def test_connect_calls_start_exactly_once_with_missing_file() -> None:
    service = BlinkService("user@example.com", "pw")
    blink = _make_blink_mock(start_result=True)

    with (
        patch("blink_service.os.path.exists", return_value=False),
        patch("blink_service.Blink", return_value=blink),
        patch("blink_service.Auth", _FakeAuth),
    ):
        await service.connect()

    blink.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_calls_start_exactly_once_with_stale_file() -> None:
    service = BlinkService("user@example.com", "pw")
    blink = _make_blink_mock(start_result=True)

    with (
        patch("blink_service.json_load", new=AsyncMock(return_value=None)),
        patch("blink_service.os.path.exists", return_value=True),
        patch("blink_service.Blink", return_value=blink),
        patch("blink_service.Auth", _FakeAuth),
    ):
        await service.connect()

    blink.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_raises_two_fa_returns_needs_2fa_and_retains_blink() -> (
    None
):
    from blink_service import BlinkTwoFARequiredError

    service = BlinkService("user@example.com", "pw")
    blink = _make_blink_mock()
    blink.start = AsyncMock(side_effect=BlinkTwoFARequiredError())

    with (
        patch("blink_service.os.path.exists", return_value=False),
        patch("blink_service.Blink", return_value=blink),
        patch("blink_service.Auth", _FakeAuth),
    ):
        result = await service.connect()

    assert result == ConnectResult.NEEDS_2FA
    assert service._blink is blink


@pytest.mark.asyncio
async def test_connect_start_returns_false_gives_failed() -> None:
    service = BlinkService("user@example.com", "pw")
    blink = _make_blink_mock(start_result=False)

    with (
        patch("blink_service.os.path.exists", return_value=False),
        patch("blink_service.Blink", return_value=blink),
        patch("blink_service.Auth", _FakeAuth),
    ):
        result = await service.connect()

    assert result == ConnectResult.FAILED


@pytest.mark.asyncio
async def test_submit_2fa_code_success_returns_true() -> None:
    service = BlinkService("user@example.com", "pw")
    blink = _make_blink_mock()
    service._blink = blink
    blink.send_2fa_code = AsyncMock(return_value=True)

    result = await service.submit_2fa_code("123456")

    assert result is True
    blink.send_2fa_code.assert_awaited_once_with("123456")


@pytest.mark.asyncio
async def test_submit_2fa_code_failure_returns_false() -> None:
    service = BlinkService("user@example.com", "pw")
    blink = _make_blink_mock()
    service._blink = blink
    blink.send_2fa_code = AsyncMock(return_value=False)

    result = await service.submit_2fa_code("wrong")

    assert result is False


@pytest.mark.asyncio
async def test_save_credentials_calls_blink_save() -> None:
    service = BlinkService("user@example.com", "pw")
    blink = _make_blink_mock()
    service._blink = blink

    await service.save_credentials()

    blink.save.assert_awaited_once_with(BlinkService.CREDENTIALS_FILE)


def _make_camera(name, arm=True, online=True, battery="ok"):
    cam = MagicMock()
    cam.name = name
    cam.camera_id = "1"
    cam.network_id = "10"
    cam.product_type = "catalina"
    cam.online = online
    cam.arm = arm
    cam.battery = battery
    return cam


def test_list_all_cameras_returns_camera_info() -> None:
    service = BlinkService("user@example.com", "pw")
    blink = _make_blink_mock(
        cameras={"Backyard": _make_camera("Backyard", arm=True)}
    )
    service._blink = blink

    cameras = service.list_all_cameras()

    assert cameras == [
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


def test_list_all_cameras_coerces_unknown_arm_string_to_false() -> None:
    service = BlinkService("user@example.com", "pw")
    blink = _make_blink_mock(
        cameras={"Backyard": _make_camera("Backyard", arm="unknown")}
    )
    service._blink = blink

    cameras = service.list_all_cameras()

    assert cameras[0].armed is False


@pytest.mark.asyncio
async def test_arm_cameras_calls_async_arm_true() -> None:
    service = BlinkService("user@example.com", "pw")
    cam = _make_camera("Backyard")
    cam.async_arm = AsyncMock()
    blink = _make_blink_mock(cameras={"Backyard": cam})
    service._blink = blink

    result = await service.arm_cameras(["Backyard"])

    cam.async_arm.assert_awaited_once_with(True)
    assert result == {"Backyard": True}


@pytest.mark.asyncio
async def test_disarm_cameras_calls_async_arm_false() -> None:
    service = BlinkService("user@example.com", "pw")
    cam = _make_camera("Backyard")
    cam.async_arm = AsyncMock()
    blink = _make_blink_mock(cameras={"Backyard": cam})
    service._blink = blink

    result = await service.disarm_cameras(["Backyard"])

    cam.async_arm.assert_awaited_once_with(False)
    assert result == {"Backyard": True}


@pytest.mark.asyncio
async def test_arm_cameras_unknown_name_returns_false_no_raise() -> None:
    service = BlinkService("user@example.com", "pw")
    blink = _make_blink_mock(cameras={})
    service._blink = blink

    result = await service.arm_cameras(["Ghost"])

    assert result == {"Ghost": False}


@pytest.mark.asyncio
async def test_snapshot_returns_bytes_from_cache() -> None:
    service = BlinkService("user@example.com", "pw")
    cam = _make_camera("Backyard")
    cam.snap_picture = AsyncMock()
    cam.image_from_cache = b"jpegbytes"
    blink = _make_blink_mock(cameras={"Backyard": cam})
    service._blink = blink

    result = await service.snapshot("Backyard")

    cam.snap_picture.assert_awaited_once()
    assert result == b"jpegbytes"


@pytest.mark.asyncio
async def test_snapshot_unknown_camera_returns_none() -> None:
    service = BlinkService("user@example.com", "pw")
    blink = _make_blink_mock(cameras={})
    service._blink = blink

    result = await service.snapshot("Ghost")

    assert result is None


@pytest.mark.asyncio
async def test_snapshot_no_cached_image_returns_none() -> None:
    service = BlinkService("user@example.com", "pw")
    cam = _make_camera("Backyard")
    cam.snap_picture = AsyncMock()
    cam.image_from_cache = None
    blink = _make_blink_mock(cameras={"Backyard": cam})
    service._blink = blink

    result = await service.snapshot("Backyard")

    assert result is None


@pytest.mark.asyncio
async def test_get_latest_clip_returns_bytes_of_most_recent() -> None:
    service = BlinkService("user@example.com", "pw")
    cam = _make_camera("Backyard")
    cam.recent_clips = [
        {"time": "2024-01-01T00:00:00", "clip": "url1"},
        {"time": "2024-01-02T00:00:00", "clip": "url2"},
    ]
    response = MagicMock()
    response.status = 200
    response.read = AsyncMock(return_value=b"videobytes")
    cam.get_video_clip = AsyncMock(return_value=response)
    blink = _make_blink_mock(cameras={"Backyard": cam})
    service._blink = blink

    result = await service.get_latest_clip("Backyard")

    cam.get_video_clip.assert_awaited_once_with(url="url2")
    assert result == b"videobytes"


@pytest.mark.asyncio
async def test_get_latest_clip_empty_recent_clips_returns_none() -> None:
    service = BlinkService("user@example.com", "pw")
    cam = _make_camera("Backyard")
    cam.recent_clips = []
    blink = _make_blink_mock(cameras={"Backyard": cam})
    service._blink = blink

    result = await service.get_latest_clip("Backyard")

    assert result is None


@pytest.mark.asyncio
async def test_get_new_motion_events_returns_new_clips() -> None:
    service = BlinkService("user@example.com", "pw")
    cam = _make_camera("Backyard")
    cam.recent_clips = [
        {"time": "2024-01-01T00:00:00", "clip": "url1"},
        {"time": "2024-01-02T00:00:00", "clip": "url2"},
    ]
    response = MagicMock()
    response.status = 200
    response.read = AsyncMock(return_value=b"videobytes")
    cam.get_video_clip = AsyncMock(return_value=response)
    blink = _make_blink_mock(cameras={"Backyard": cam})
    service._blink = blink

    events = await service.get_new_motion_events(
        {"Backyard": "2024-01-01T00:00:00"}
    )

    assert events == [
        MotionEvent(
            camera_name="Backyard",
            clip_time="2024-01-02T00:00:00",
            clip_bytes=b"videobytes",
        )
    ]


@pytest.mark.asyncio
async def test_get_new_motion_events_no_new_clips_returns_empty() -> None:
    service = BlinkService("user@example.com", "pw")
    cam = _make_camera("Backyard")
    cam.recent_clips = [{"time": "2024-01-01T00:00:00", "clip": "url1"}]
    blink = _make_blink_mock(cameras={"Backyard": cam})
    service._blink = blink

    events = await service.get_new_motion_events(
        {"Backyard": "2024-01-01T00:00:00"}
    )

    assert events == []


@pytest.mark.asyncio
async def test_refresh_calls_blink_refresh_once() -> None:
    service = BlinkService("user@example.com", "pw")
    blink = _make_blink_mock()
    service._blink = blink

    await service.refresh()

    blink.refresh.assert_awaited_once()


def test_is_connected_true_when_cameras_populated() -> None:
    service = BlinkService("user@example.com", "pw")
    blink = _make_blink_mock(cameras={"Backyard": _make_camera("Backyard")})
    service._blink = blink

    assert service.is_connected is True


def test_is_connected_false_when_no_blink_instance() -> None:
    service = BlinkService("user@example.com", "pw")
    assert service.is_connected is False


def test_is_connected_false_when_cameras_empty() -> None:
    service = BlinkService("user@example.com", "pw")
    blink = _make_blink_mock(cameras={})
    service._blink = blink

    assert service.is_connected is False


# --- Locking (codereview.md H-1) ---


@pytest.mark.asyncio
async def test_refresh_and_snapshot_do_not_run_concurrently() -> None:
    """Concurrent calls into BlinkService must be serialized by the
    internal lock — a snapshot must not interleave with a refresh."""
    service = BlinkService("user@example.com", "pw")
    cam = _make_camera("Backyard")
    cam.snap_picture = AsyncMock()
    cam.image_from_cache = b"jpeg"
    blink = _make_blink_mock(cameras={"Backyard": cam})
    service._blink = blink

    events: list[str] = []

    async def slow_refresh():
        events.append("refresh:start")
        await asyncio.sleep(0.05)
        events.append("refresh:end")

    blink.refresh = slow_refresh

    async def tracked_snap_picture():
        events.append("snapshot:start")
        events.append("snapshot:end")

    cam.snap_picture = tracked_snap_picture

    await asyncio.gather(service.refresh(), service.snapshot("Backyard"))

    # The two operations must not interleave — one completes fully
    # before the other starts.
    assert events in (
        ["refresh:start", "refresh:end", "snapshot:start", "snapshot:end"],
        ["snapshot:start", "snapshot:end", "refresh:start", "refresh:end"],
    )


# --- Timeouts (codereview.md H-2) ---


@pytest.mark.asyncio
async def test_refresh_times_out_raises_blink_timeout_error() -> None:
    service = BlinkService("user@example.com", "pw")
    blink = _make_blink_mock()

    async def hang():
        await asyncio.sleep(10)

    blink.refresh = hang
    service._blink = blink

    with (
        patch("blink_service.BLINK_CALL_TIMEOUT_SECONDS", 0.01),
        pytest.raises(BlinkTimeoutError),
    ):
        await service.refresh()


@pytest.mark.asyncio
async def test_connect_times_out_raises_blink_timeout_error() -> None:
    service = BlinkService("user@example.com", "pw")
    blink = _make_blink_mock()

    async def hang():
        await asyncio.sleep(10)

    blink.start = hang

    with (
        patch("blink_service.os.path.exists", return_value=False),
        patch("blink_service.Blink", return_value=blink),
        patch("blink_service.Auth", _FakeAuth),
        patch("blink_service.BLINK_CALL_TIMEOUT_SECONDS", 0.01),
        pytest.raises(BlinkTimeoutError),
    ):
        await service.connect()


# --- Credential file permissions (codereview.md H-5) ---


@pytest.mark.asyncio
async def test_save_credentials_restricts_file_permissions() -> None:
    service = BlinkService("user@example.com", "pw")
    blink = _make_blink_mock()
    service._blink = blink

    with patch("blink_service.os.chmod") as mock_chmod:
        await service.save_credentials()

    if __import__("os").name == "posix":
        mock_chmod.assert_called_once_with(BlinkService.CREDENTIALS_FILE, 0o600)


# --- Motion event camera filtering (codereview.md M-2) ---


@pytest.mark.asyncio
async def test_get_new_motion_events_filters_to_camera_names() -> None:
    service = BlinkService("user@example.com", "pw")
    controlled = _make_camera("Backyard")
    controlled.recent_clips = [{"time": "2024-01-02T00:00:00", "clip": "c1"}]
    response = MagicMock()
    response.status = 200
    response.read = AsyncMock(return_value=b"video")
    controlled.get_video_clip = AsyncMock(return_value=response)

    uncontrolled = _make_camera("Garage")
    uncontrolled.recent_clips = [{"time": "2024-01-02T00:00:00", "clip": "c2"}]
    uncontrolled.get_video_clip = AsyncMock()

    blink = _make_blink_mock(
        cameras={"Backyard": controlled, "Garage": uncontrolled}
    )
    service._blink = blink

    events = await service.get_new_motion_events({}, camera_names=["Backyard"])

    assert [e.camera_name for e in events] == ["Backyard"]
    uncontrolled.get_video_clip.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_new_motion_events_without_filter_checks_all_cameras() -> (
    None
):
    service = BlinkService("user@example.com", "pw")
    cam_a = _make_camera("Backyard")
    cam_a.recent_clips = [{"time": "2024-01-02T00:00:00", "clip": "c1"}]
    response = MagicMock()
    response.status = 200
    response.read = AsyncMock(return_value=b"video")
    cam_a.get_video_clip = AsyncMock(return_value=response)

    cam_b = _make_camera("Garage")
    cam_b.recent_clips = [{"time": "2024-01-02T00:00:00", "clip": "c2"}]
    cam_b.get_video_clip = AsyncMock(return_value=response)

    blink = _make_blink_mock(cameras={"Backyard": cam_a, "Garage": cam_b})
    service._blink = blink

    events = await service.get_new_motion_events({})

    assert {e.camera_name for e in events} == {"Backyard", "Garage"}
