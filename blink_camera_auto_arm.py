import asyncio
import os
import platform
import subprocess
import time
from typing import List, Callable

from blink_camera_controller import BlinkCameraController
from blink_telegram_bot import BlinkTelegramBot

from dotenv import load_dotenv
from globals import Globals


async def start_main_loop(
    camera: BlinkCameraController, telegram: BlinkTelegramBot
) -> None:
    """Main loop to handle events and perform camera arming."""
    any_at_home = get_ip_checks_for_address_list(
        camera.ips_to_monitor, camera.absence_checks_amount
    )
    camera_instance = None
    while True:
        await asyncio.sleep(camera.ips_checking_interval)

        # If status requested form Telegram chat -> sync with API
        if Globals.is_status_sync_requested:
            if camera.is_api_connected and camera_instance:
                await camera.sync_status()
                Globals.is_camera_armed = camera_instance.arm
            Globals.is_status_sync_requested = False

        # App is disabled -> skip loop until enabled
        if Globals.is_app_disabled:
            continue

        # Camera API is not connected -> try to connect and save credentials
        if not camera.is_api_connected:
            await connect_to_blink_api(camera, telegram)
            if camera.is_api_connected:
                await camera.save_credentials_to_file()

        # Could not connect to camera API -> disable app
        if not camera.is_api_connected:
            Globals.is_app_disabled = True
            telegram.send_message(
                'Could not connect to Blink API. Disabling autoarm app.'
            )
            continue

        # Get 1st camera instance
        if not camera_instance:
            camera_instance = camera.get_camera_instance()
            telegram.send_message(
                f'Successfully connected to camera: {camera_instance.name}.'
            )

        # Arming/disarming camera on ip availability
        if not any([check() for check in any_at_home]):  # if nobody home
            if not camera_instance.arm:
                await camera.arm_camera()
                telegram.send_message(
                    f'Nobody at home.\nArming camera.\n'
                    f'Status: {"armed" if camera_instance.arm else "disarmed"}.'
                )
                Globals.is_camera_armed = camera_instance.arm
                Globals.time_of_last_status_change = time.time()
        else:
            if camera_instance.arm:
                await camera.disarm_camera()
                telegram.send_message(
                    f'Somebody returned home.\nDisarming camera.\n'
                    f'Status: {"armed" if camera_instance.arm else "disarmed"}.'
                )
                Globals.is_camera_armed = camera_instance.arm
                Globals.time_of_last_status_change = time.time()


async def connect_to_blink_api(
    camera: BlinkCameraController, telegram: BlinkTelegramBot
) -> None:
    """Establish connection to Blink API."""
    await camera.connect_with_credentials_file()
    if not camera.is_api_connected:
        await camera.connect_with_sms_token()
        auth_key = await request_blink_sms_auth_key(telegram)
        if auth_key:
            await camera.authenticate_connection_with_sms_token(auth_key)


async def request_blink_sms_auth_key(telegram: BlinkTelegramBot) -> str:
    """Request in telegram and return Blink auth key from SMS."""
    telegram.send_message(
        'Blink auth key is required. Please enter the key from sms:'
    )
    Globals.is_auth_key_required = True

    start_time = time.time()
    duration = 300
    while Globals.auth_key is None and time.time() - start_time < duration:
        await asyncio.sleep(5)

    auth_key = None
    if Globals.auth_key:
        auth_key = Globals.auth_key
        Globals.auth_key = None
        telegram.send_message("Thank you, auth key received.")
    Globals.is_auth_key_required = False
    return auth_key


def ping(host: str) -> bool:
    """Returns True if host (str) responds to a ping request."""

    # Flag for define ping packets number
    param = '-n' if platform.system().lower() == 'windows' else '-c'

    # Building the command. Ex: "ping -c 1 google.com"
    command = ['ping', param, '1', host]
    return (
        subprocess.call(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        == 0
    )


def get_host_alive_check(address: str, attempts: int = 5) -> Callable:
    """Returns closure which stores several ping attempts and
    returns False if host doesn't answer more than <attempts> number."""
    ping_results = []

    def inner():
        nonlocal ping_results
        ping_results.append(ping(address))
        Globals.ips_status[address] = ping_results[-1]
        ping_results = ping_results[-attempts:]
        return (
            True if len(ping_results) < attempts or any(ping_results) else False
        )

    return inner


def get_ip_checks_for_address_list(
    addresses: list, attempts: int
) -> List[Callable]:
    """Generates list of closure check functions for particular ip.
    Closure stores state of previous executions."""
    return [get_host_alive_check(ip, attempts) for ip in addresses]


async def main():
    load_dotenv()
    account_name = os.getenv('BLINK_CAMERA_AUTO_ARM_USERNAME')
    account_password = os.getenv('BLINK_CAMERA_AUTO_ARM_PASSWORD')
    ips_to_monitor = os.getenv('BLINK_CAMERA_AUTO_ARM_DEVICES')
    telegram_bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID')

    if not account_name or not account_password or not ips_to_monitor:
        raise KeyError('Environment file does not contain required fields.')

    # Extract ip address list and set initial status to False (Offline).
    ips_to_monitor = list(map(str.strip, ips_to_monitor.split(',')))
    Globals.ips_status = {ip: False for ip in ips_to_monitor}

    camera_controller = BlinkCameraController(
        account_name, account_password, ips_to_monitor
    )
    telegram_bot = BlinkTelegramBot(telegram_bot_token, telegram_chat_id)

    main_loop_task = start_main_loop(camera_controller, telegram_bot)
    await asyncio.gather(telegram_bot.start_listener_task(), main_loop_task)


if __name__ == '__main__':
    asyncio.run(main())
