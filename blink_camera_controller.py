import asyncio
import os

from aiohttp import ClientSession

from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth
from blinkpy.helpers.util import json_load


class BlinkCameraController:
    """Convenience class to control the blinking camera."""

    _CREDENTIALS_FILE = 'blink_credentials.json'

    def __init__(
        self,
        account_name,
        account_password,
        ips_to_monitor,
        monitored_camera_id=0,
        absence_checks_amount=5,
        ips_checking_interval=60,
        blink_api_safety_interval=60,
    ):
        self.account_name = account_name
        self.account_password = account_password
        self.ips_to_monitor = ips_to_monitor
        self.monitored_camera_id = monitored_camera_id
        self.ips_checking_interval = ips_checking_interval
        self.absence_checks_amount = absence_checks_amount
        self.blink_api_safety_interval = blink_api_safety_interval
        self.blink_connection = None

    async def connect_with_credentials_file(self) -> None:
        """Establish connection to Blink server with credentials file."""
        if not os.path.exists(self._CREDENTIALS_FILE):
            return
        await asyncio.sleep(self.blink_api_safety_interval)

        blink_ = Blink(session=ClientSession())
        blink_.auth = Auth(await json_load(self._CREDENTIALS_FILE))
        await blink_.start()

        self.blink_connection = blink_

    async def connect_with_sms_token(
        self, account_name: str = None, account_password: str = None
    ) -> None:
        """Establish connection to Blink server with SMS token."""
        if account_name is None:
            account_name = self.account_name
        if account_password is None:
            account_password = self.account_password

        if os.path.exists(self._CREDENTIALS_FILE):
            self._delete_file(self._CREDENTIALS_FILE)

        await asyncio.sleep(self.blink_api_safety_interval)

        blink_ = Blink(session=ClientSession())
        blink_.auth = Auth(
            {'username': account_name, 'password': account_password},
            no_prompt=True,
        )
        await blink_.start()
        self.blink_connection = blink_

    async def authenticate_connection_with_sms_token(
        self, auth_key: str
    ) -> None:
        """Authenticate connection with SMS token."""
        await self.blink_connection.auth.send_auth_key(
            self.blink_connection, auth_key
        )
        await self.blink_connection.setup_post_verify()

    def get_camera_instance(self, id_: None | int = None):
        """Get camera instance by id from cameras list in Blink connection."""
        if id_ is None:
            id_ = self.monitored_camera_id
        return list(self.blink_connection.cameras.values())[id_]

    async def arm_camera(self, id_: None | int = None) -> None:
        """Arm camera by camera id."""
        if id_ is None:
            id_ = self.monitored_camera_id
        await self._set_camera_arming_status(id_, True)

    async def disarm_camera(self, id_: None | int = None) -> None:
        """Disarm camera by camera id."""
        if id_ is None:
            id_ = self.monitored_camera_id
        await self._set_camera_arming_status(id_, False)

    async def _set_camera_arming_status(self, id_, set_armed: bool) -> None:
        """Set camera armed/disarmed."""
        await self.get_camera_instance(id_).async_arm(set_armed)
        await self.sync_status()

    async def sync_status(self) -> None:
        """Synchronize local Blink connection object with server."""
        await asyncio.sleep(self.blink_api_safety_interval)
        await self.blink_connection.refresh()

    async def save_credentials_to_file(self) -> None:
        """Save credentials to file. It helps to avoid request for SMS token."""
        await self.blink_connection.save(self._CREDENTIALS_FILE)

    @property
    def is_api_connected(self):
        """Return True if Blink API is connected and authenticated."""
        return (
            self.blink_connection
            and self.blink_connection.cameras
            and len(self.blink_connection.cameras) > 0
        )

    @staticmethod
    def _delete_file(file_path: str) -> None:
        """Delete file."""
        try:
            os.remove(file_path)
            print(f"File '{file_path}' deleted successfully.")
        except FileNotFoundError:
            print(f"File '{file_path}' not found.")
        except PermissionError:
            print(f"You lack permission to delete '{file_path}'.")
        except OSError as e:
            print(f"Error deleting file: {e}")
