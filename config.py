import ipaddress
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Application data directory — resolved from this file's location so that
# config.json is always found/created here regardless of the process's
# current working directory (e.g. when started by a cron watchdog that
# does not `cd` into the project first). See codereview.md CR-4.
APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = APP_DIR / "config.json"

_MIN_ABSENCE_CHECKS = 1
_MAX_ABSENCE_CHECKS = 1000
_MIN_PING_INTERVAL_SECONDS = 1
_MAX_PING_INTERVAL_SECONDS = 86400


@dataclass
class AppConfig:
    """Combined runtime configuration: secrets from `.env` + mutable
    settings from `config.json`."""

    # Loaded from .env — never written to config.json
    blink_username: str
    blink_password: str
    telegram_bot_token: str
    telegram_chat_id: int
    telegram_allowed_user_id: int

    # Loaded from config.json — mutable at runtime via Telegram commands
    monitored_ips: list[str] = field(default_factory=list)
    controlled_cameras: list[str] = field(default_factory=list)
    absence_checks: int = 5
    ping_interval_seconds: int = 60
    motion_alerts_enabled: bool = False


class Config:
    """Loads .env + config.json and provides atomic, validated save."""

    CONFIG_FILE = str(DEFAULT_CONFIG_FILE)

    _MUTABLE_FIELDS = (
        "monitored_ips",
        "controlled_cameras",
        "absence_checks",
        "ping_interval_seconds",
        "motion_alerts_enabled",
    )

    def __init__(self, config_file: str | None = None) -> None:
        """Store the config.json path (defaults to an absolute path next
        to this module, so it is independent of the process's cwd)."""
        self.config_file = config_file or self.CONFIG_FILE

    def load(self) -> AppConfig:
        """Load config from .env and config.json.

        Creates config.json with empty/default values on first run —
        monitored IPs and controlled cameras are then managed entirely
        via Telegram commands (see README.md's configuration reference).

        Raises ValueError if config.json exists but is malformed JSON or
        fails schema validation (unknown fields, wrong types, invalid IP
        literals, or out-of-range numeric values) — a corrupt config file
        must be fixed or removed by an operator rather than silently
        replaced, since it may hold hand-verified monitored IPs/cameras.
        """
        env = self._load_required_env()

        if os.path.exists(self.config_file):
            mutable = self._read_config_file()
        else:
            mutable = self._defaults()
            self._write_config_file(mutable)

        return AppConfig(
            blink_username=env["blink_username"],
            blink_password=env["blink_password"],
            telegram_bot_token=env["telegram_bot_token"],
            telegram_chat_id=env["telegram_chat_id"],
            telegram_allowed_user_id=env["telegram_allowed_user_id"],
            monitored_ips=mutable["monitored_ips"],
            controlled_cameras=mutable["controlled_cameras"],
            absence_checks=mutable["absence_checks"],
            ping_interval_seconds=mutable["ping_interval_seconds"],
            motion_alerts_enabled=mutable["motion_alerts_enabled"],
        )

    def save(self, cfg: AppConfig) -> None:
        """Atomically write mutable fields of AppConfig to config.json.

        Raises ValueError if the fields on `cfg` fail schema validation —
        callers should validate/build a candidate config before mutating
        any long-lived object (see codereview.md H-4).
        """
        data = {name: getattr(cfg, name) for name in self._MUTABLE_FIELDS}
        data = self._validate_mutable(data)
        self._write_config_file(data)

    def _read_config_file(self) -> dict:
        """Read config.json, filling missing keys with defaults.

        Raises ValueError on malformed JSON or schema validation failure.
        """
        try:
            with open(self.config_file, encoding="utf-8") as f:
                raw = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Config file '{self.config_file}' contains malformed "
                f"JSON: {e}. Fix or remove the file and restart."
            ) from e

        if not isinstance(raw, dict):
            raise ValueError(
                f"Config file '{self.config_file}' must contain a JSON "
                f"object, got {type(raw).__name__}."
            )

        defaults = self._defaults()
        merged = {**defaults, **raw}
        return self._validate_mutable(merged)

    def _write_config_file(self, data: dict) -> None:
        """Atomically write data to config.json (temp file + fsync +
        rename), then fsync the containing directory and restrict file
        permissions to owner-only where supported.

        Durability/permissions per codereview.md M-3 and H-5: a bare
        os.replace() protects readers from a partial file but does not
        survive sudden power loss on the router target without fsync.
        """
        directory = os.path.dirname(self.config_file) or "."
        fd, tmp_path = tempfile.mkstemp(
            dir=directory, suffix=".tmp", prefix="config_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.config_file)
            _fsync_directory(directory)
            _restrict_permissions(self.config_file)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    @staticmethod
    def _defaults() -> dict:
        """Return the default (empty) mutable config.json contents."""
        return {
            "monitored_ips": [],
            "controlled_cameras": [],
            "absence_checks": 5,
            "ping_interval_seconds": 60,
            "motion_alerts_enabled": False,
        }

    @staticmethod
    def _validate_mutable(data: dict) -> dict:
        """Validate the full mutable config schema.

        Enforces known fields only, correct types, unique valid IP
        literals, unique non-empty camera names, and bounded numeric
        ranges. Raises ValueError with a descriptive message on the
        first violation found. See codereview.md H-3.
        """
        known_fields = set(Config._MUTABLE_FIELDS)
        unknown = set(data) - known_fields
        if unknown:
            raise ValueError(
                f"Unknown config field(s): {', '.join(sorted(unknown))}."
            )

        monitored_ips = _validate_unique_ip_list(
            data["monitored_ips"], "monitored_ips"
        )
        controlled_cameras = _validate_unique_name_list(
            data["controlled_cameras"], "controlled_cameras"
        )

        absence_checks = data["absence_checks"]
        if (
            not isinstance(absence_checks, int)
            or isinstance(absence_checks, bool)
            or not (
                _MIN_ABSENCE_CHECKS <= absence_checks <= _MAX_ABSENCE_CHECKS
            )
        ):
            raise ValueError(
                "'absence_checks' must be an integer between "
                f"{_MIN_ABSENCE_CHECKS} and {_MAX_ABSENCE_CHECKS}, "
                f"got {absence_checks!r}."
            )

        ping_interval_seconds = data["ping_interval_seconds"]
        if (
            not isinstance(ping_interval_seconds, int)
            or isinstance(ping_interval_seconds, bool)
            or not (
                _MIN_PING_INTERVAL_SECONDS
                <= ping_interval_seconds
                <= _MAX_PING_INTERVAL_SECONDS
            )
        ):
            raise ValueError(
                "'ping_interval_seconds' must be an integer between "
                f"{_MIN_PING_INTERVAL_SECONDS} and "
                f"{_MAX_PING_INTERVAL_SECONDS}, got "
                f"{ping_interval_seconds!r}."
            )

        motion_alerts_enabled = data["motion_alerts_enabled"]
        if not isinstance(motion_alerts_enabled, bool):
            raise ValueError(
                "'motion_alerts_enabled' must be a boolean, got "
                f"{motion_alerts_enabled!r}."
            )

        return {
            "monitored_ips": monitored_ips,
            "controlled_cameras": controlled_cameras,
            "absence_checks": absence_checks,
            "ping_interval_seconds": ping_interval_seconds,
            "motion_alerts_enabled": motion_alerts_enabled,
        }

    @staticmethod
    def _load_required_env() -> dict:
        """Load and validate required .env fields for AppConfig."""
        required_str = {
            "blink_username": "BLINK_CAMERA_AUTO_ARM_USERNAME",
            "blink_password": "BLINK_CAMERA_AUTO_ARM_PASSWORD",
            "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
        }
        required_int = {
            "telegram_chat_id": "TELEGRAM_CHAT_ID",
            "telegram_allowed_user_id": "TELEGRAM_ALLOWED_USER_ID",
        }

        result: dict = {}
        for field_name, env_name in required_str.items():
            value = os.getenv(env_name)
            if not value:
                raise ValueError(
                    f"Environment variable '{env_name}' is required "
                    f"but not set."
                )
            result[field_name] = value

        for field_name, env_name in required_int.items():
            value = os.getenv(env_name)
            if not value:
                raise ValueError(
                    f"Environment variable '{env_name}' is required "
                    f"but not set."
                )
            try:
                result[field_name] = int(value)
            except ValueError as e:
                raise ValueError(
                    f"Environment variable '{env_name}' must be an "
                    f"integer, got: {value!r}"
                ) from e

        return result


def _validate_unique_ip_list(value: object, field_name: str) -> list[str]:
    """Validate `value` is a list of unique valid IP address literals."""
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError(f"'{field_name}' must be a list of strings.")

    seen: set[str] = set()
    for item in value:
        try:
            ipaddress.ip_address(item)
        except ValueError as e:
            raise ValueError(
                f"'{field_name}' contains an invalid IP address: " f"{item!r}."
            ) from e
        if item in seen:
            raise ValueError(
                f"'{field_name}' contains duplicate entry: {item!r}."
            )
        seen.add(item)
    return list(value)


def _validate_unique_name_list(value: object, field_name: str) -> list[str]:
    """Validate `value` is a list of unique, non-empty string names."""
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError(f"'{field_name}' must be a list of strings.")

    seen: set[str] = set()
    for item in value:
        if not item.strip():
            raise ValueError(f"'{field_name}' contains an empty name.")
        if item in seen:
            raise ValueError(
                f"'{field_name}' contains duplicate entry: {item!r}."
            )
        seen.add(item)
    return list(value)


def _fsync_directory(directory: str) -> None:
    """Best-effort fsync of a directory so a rename survives power loss.

    Not supported on Windows (dev-only platform for this project); the
    router deployment target is embedded Linux, where this matters.
    """
    if os.name != "posix":
        return
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        _LOGGER.warning(
            "Could not fsync directory '%s' after config write.", directory
        )


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
