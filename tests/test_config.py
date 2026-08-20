"""Tests for config.py — AppConfig dataclass + Config loader/saver."""

import json
import os
from pathlib import Path

import pytest

from config import AppConfig, Config

REQUIRED_ENV = {
    "BLINK_CAMERA_AUTO_ARM_USERNAME": "test@example.com",
    "BLINK_CAMERA_AUTO_ARM_PASSWORD": "secret",
    "TELEGRAM_BOT_TOKEN": "123:ABC",
    "TELEGRAM_CHAT_ID": "-100123456789",
    "TELEGRAM_ALLOWED_USER_ID": "999",
}


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def test_load_creates_config_json_with_defaults_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_required_env(monkeypatch)
    config_file = tmp_path / "config.json"
    config = Config(config_file=str(config_file))

    cfg = config.load()

    assert isinstance(cfg, AppConfig)
    assert cfg.blink_username == "test@example.com"
    assert cfg.blink_password == "secret"
    assert cfg.telegram_bot_token == "123:ABC"
    assert cfg.telegram_chat_id == -100123456789
    assert cfg.telegram_allowed_user_id == 999
    assert cfg.monitored_ips == []
    assert cfg.controlled_cameras == []
    assert cfg.absence_checks == 5
    assert cfg.ping_interval_seconds == 60
    assert cfg.motion_alerts_enabled is False
    assert config_file.exists()

    written = json.loads(config_file.read_text())
    assert written["monitored_ips"] == []


def test_load_reads_existing_config_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_required_env(monkeypatch)
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps(
            {
                "monitored_ips": ["192.168.0.99"],
                "controlled_cameras": ["Backyard"],
                "absence_checks": 3,
                "ping_interval_seconds": 30,
                "motion_alerts_enabled": True,
            }
        )
    )
    config = Config(config_file=str(config_file))

    cfg = config.load()

    assert cfg.monitored_ips == ["192.168.0.99"]
    assert cfg.controlled_cameras == ["Backyard"]
    assert cfg.absence_checks == 3
    assert cfg.ping_interval_seconds == 30
    assert cfg.motion_alerts_enabled is True


def test_load_with_partial_config_json_falls_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_required_env(monkeypatch)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"monitored_ips": ["192.168.0.5"]}))
    config = Config(config_file=str(config_file))

    cfg = config.load()

    assert cfg.monitored_ips == ["192.168.0.5"]
    assert cfg.controlled_cameras == []
    assert cfg.absence_checks == 5
    assert cfg.ping_interval_seconds == 60
    assert cfg.motion_alerts_enabled is False


@pytest.mark.parametrize("missing_key", list(REQUIRED_ENV.keys()))
def test_load_raises_value_error_when_required_env_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing_key: str
) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.delenv(missing_key, raising=False)
    config_file = tmp_path / "config.json"
    config = Config(config_file=str(config_file))

    with pytest.raises(ValueError, match=missing_key):
        config.load()


def test_save_writes_atomically_and_round_trips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_required_env(monkeypatch)
    config_file = tmp_path / "config.json"
    config = Config(config_file=str(config_file))
    cfg = config.load()

    cfg.monitored_ips = ["192.168.0.42"]
    cfg.controlled_cameras = ["Front Door"]
    cfg.absence_checks = 7
    cfg.ping_interval_seconds = 45
    cfg.motion_alerts_enabled = True

    config.save(cfg)

    assert config_file.exists()
    # No leftover temp files after a successful save.
    assert list(tmp_path.glob("*.tmp")) == []

    reloaded = config.load()
    assert reloaded.monitored_ips == ["192.168.0.42"]
    assert reloaded.controlled_cameras == ["Front Door"]
    assert reloaded.absence_checks == 7
    assert reloaded.ping_interval_seconds == 45
    assert reloaded.motion_alerts_enabled is True
    # Secrets from .env must never be written to config.json.
    written = json.loads(config_file.read_text())
    assert "blink_username" not in written
    assert "blink_password" not in written
    assert "telegram_bot_token" not in written


def test_save_does_not_corrupt_existing_file_on_write_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_required_env(monkeypatch)
    config_file = tmp_path / "config.json"
    config = Config(config_file=str(config_file))
    cfg = config.load()
    original_content = config_file.read_text()

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr("json.dump", _boom)

    with pytest.raises(OSError):
        config.save(cfg)

    # Original file must be untouched — atomic write never replaced it.
    assert config_file.read_text() == original_content
    # No leftover temp files after a failed save.
    assert list(tmp_path.glob("*.tmp")) == []


# --- Schema validation ---


def test_load_malformed_json_raises_value_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_required_env(monkeypatch)
    config_file = tmp_path / "config.json"
    config_file.write_text("{not valid json")
    config = Config(config_file=str(config_file))

    with pytest.raises(ValueError, match="malformed"):
        config.load()


def test_load_non_object_json_raises_value_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_required_env(monkeypatch)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(["not", "an", "object"]))
    config = Config(config_file=str(config_file))

    with pytest.raises(ValueError, match="JSON object"):
        config.load()


def test_load_unknown_field_raises_value_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_required_env(monkeypatch)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"bogus_field": 1}))
    config = Config(config_file=str(config_file))

    with pytest.raises(ValueError, match="Unknown config field"):
        config.load()


def test_load_invalid_ip_in_monitored_ips_raises_value_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_required_env(monkeypatch)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"monitored_ips": ["not-an-ip"]}))
    config = Config(config_file=str(config_file))

    with pytest.raises(ValueError, match="invalid IP"):
        config.load()


def test_load_duplicate_ip_raises_value_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_required_env(monkeypatch)
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"monitored_ips": ["192.168.0.1", "192.168.0.1"]})
    )
    config = Config(config_file=str(config_file))

    with pytest.raises(ValueError, match="duplicate"):
        config.load()


def test_load_non_list_monitored_ips_raises_value_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_required_env(monkeypatch)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"monitored_ips": "192.168.0.1"}))
    config = Config(config_file=str(config_file))

    with pytest.raises(ValueError, match="monitored_ips"):
        config.load()


def test_load_empty_camera_name_raises_value_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_required_env(monkeypatch)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"controlled_cameras": ["  "]}))
    config = Config(config_file=str(config_file))

    with pytest.raises(ValueError, match="empty name"):
        config.load()


@pytest.mark.parametrize("bad_value", [0, -1, 1001, "5", True])
def test_load_absence_checks_out_of_range_raises_value_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_value: object
) -> None:
    _set_required_env(monkeypatch)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"absence_checks": bad_value}))
    config = Config(config_file=str(config_file))

    with pytest.raises(ValueError, match="absence_checks"):
        config.load()


@pytest.mark.parametrize("bad_value", [0, -1, 86401, "60", False])
def test_load_ping_interval_out_of_range_raises_value_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_value: object
) -> None:
    _set_required_env(monkeypatch)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"ping_interval_seconds": bad_value}))
    config = Config(config_file=str(config_file))

    with pytest.raises(ValueError, match="ping_interval_seconds"):
        config.load()


def test_load_motion_alerts_enabled_non_bool_raises_value_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_required_env(monkeypatch)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"motion_alerts_enabled": "yes"}))
    config = Config(config_file=str(config_file))

    with pytest.raises(ValueError, match="motion_alerts_enabled"):
        config.load()


def test_save_rejects_invalid_ip_leaving_file_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_required_env(monkeypatch)
    config_file = tmp_path / "config.json"
    config = Config(config_file=str(config_file))
    cfg = config.load()
    original_content = config_file.read_text()

    cfg.monitored_ips = ["not-an-ip"]
    with pytest.raises(ValueError):
        config.save(cfg)

    assert config_file.read_text() == original_content


def test_write_config_file_fsyncs_and_restricts_permissions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Config writes must fsync the file (M-3) and restrict permissions
    to owner-only where supported (H-5)."""
    _set_required_env(monkeypatch)
    config_file = tmp_path / "config.json"
    config = Config(config_file=str(config_file))

    fsync_calls: list[int] = []
    monkeypatch.setattr("os.fsync", lambda fd: fsync_calls.append(fd) or None)
    chmod_calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "os.chmod",
        lambda path, mode: chmod_calls.append((str(path), mode)),
    )

    config.load()

    assert fsync_calls  # file (and directory, on posix) fsync attempted
    if os.name == "posix":
        assert chmod_calls == [(str(config_file), 0o600)]


def test_default_config_file_is_absolute_path_next_to_module() -> None:
    """config.json must resolve to an absolute path derived from this
    module's location, independent of the process's cwd."""
    assert os.path.isabs(Config.CONFIG_FILE)
    assert os.path.basename(Config.CONFIG_FILE) == "config.json"
