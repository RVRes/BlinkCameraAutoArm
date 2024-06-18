from dataclasses import dataclass, field


@dataclass
class Globals:
    """Global variables to exchange data between main and telegram processes."""

    auth_key: str = None
    is_auth_key_required: bool = False
    is_app_disabled: bool = False
    is_camera_armed: bool = False
    is_status_sync_requested: bool = False
    ips_status: dict = field(default_factory=dict)
    time_of_last_status_change: None | float = None
