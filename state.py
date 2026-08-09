from dataclasses import dataclass, field


@dataclass
class AppState:
    """Runtime-only cross-task signals. Reset on every app start.

    Never persisted. Instantiated once in main() and passed by reference
    to all modules — no class-level singleton mutation, no globals.
    """

    is_app_enabled: bool = True
    # 2FA handshake
    is_2fa_pending: bool = False
    received_2fa_code: str | None = None
    # Live caches (updated by main loop; read by bot handlers)
    ip_ping_status: dict[str, bool | None] = field(default_factory=dict)
    camera_armed_status: dict[str, bool] = field(default_factory=dict)
    commanded_camera_states: dict[str, bool] = field(default_factory=dict)
    time_of_last_arm_change: float | None = None
    # Health signal (codereview.md L-3): timestamp of the most recent
    # main-loop iteration that actually ran (app enabled), so a stalled
    # loop can be distinguished from "process alive but wedged" via
    # /camerabot status — the watchdog only confirms the process exists,
    # not that it's making progress.
    time_of_last_iteration: float | None = None
