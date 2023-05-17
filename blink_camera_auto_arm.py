import asyncio

from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth
from time import sleep, localtime, strftime
import platform  # For getting the operating system name
import subprocess  # For executing a shell command
from os import getenv
from dotenv import load_dotenv
from telegram import Bot


def blink_connect(username_: str, password_: str):
    """Establish connection to Blink server."""
    sleep(SAFETY_WAIT_BLINK_API)
    blink_ = Blink()
    # Can set no_prompt when initializing auth handler
    auth = Auth({"username": username_, "password": password_}, no_prompt=True)
    blink_.auth = auth
    blink_.start()
    return blink_


def blink_cameras_list(connection):
    """Lists blink cameras."""
    cameras_ = None

    def inner(id_: int):
        nonlocal cameras_
        if not cameras_:
            cameras_ = list(connection.cameras.values())
        return cameras_[id_]

    return inner


def ping(host):
    """
    Returns True if host (str) responds to a ping request.
    Remember that a host may not respond to a ping (ICMP) request
    even if the host name is valid.
    """

    # Option for the number of packets as a function of
    param = '-n' if platform.system().lower() == 'windows' else '-c'

    # Building the command. Ex: "ping -c 1 google.com"
    command = ['ping', param, '1', host]

    return subprocess.call(command,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) == 0


def get_host_alive_check(address: str, attempts: int = 5):
    """Factory that returns closure which stores several ping attempts and
    returns False if host doesn't answer more than <attempts> number"""
    ping_results = []

    def inner():
        nonlocal ping_results
        ping_results.append(ping(address))
        ping_results = ping_results[-attempts:]
        return (True if len(ping_results) < attempts or any(ping_results)
                else False)

    return inner


def get_ip_checks_for_address_list(addresses: list, attempts: int):
    """Generates list of closure check functions for particular ip.
    Closure stores state of previous executions."""
    return [get_host_alive_check(ip, attempts) for ip in addresses]


def time_now():
    """Return formatted string with local date and time"""
    return strftime("%m/%d/%Y %H:%M:%S", localtime())


def camera_status(status: bool):
    """Converts camera status True/False to phrase Status: Armed/Disarmed"""
    return f'Status: {"Armed" if status else "Disarmed"}.'


def arm_camera(camera_, connection_):
    """Set camera to Armed"""
    camera_.arm = True
    sleep(SAFETY_WAIT_BLINK_API)
    connection_.refresh()
    msg = (f'{time_now()}: Nobody home. '
           f'Arming camera. {camera_status(camera_.arm)}')
    print(msg)
    send_message(msg)


def disarm_camera(camera_, connection_):
    """Set camera to Disarmed"""
    camera_.arm = False
    sleep(SAFETY_WAIT_BLINK_API)
    connection_.refresh()
    msg = (f'{time_now()}: Somebody returned home. '
           f'Disarming camera. {camera_status(camera_.arm)}')
    print(msg)
    send_message(msg)


def get_telegram_messenger(bot_token: str, chat_id: str):
    """
    Returns send_message function, which is used to send messages to send
    telegram notifications to specific group or user.
    :param bot_token: telegram bot token
    :param chat_id: group or user chat id
    :return:
    """
    async def send_message_coro(message: str):
        async with bot:
            await bot.send_message(text=message, chat_id=chat_id)

    def inner(message: str) -> None:
        asyncio.run(send_message_coro(message))

    bot = Bot(token=bot_token)
    return inner


def start_main_loop(username: str, password: str, home_devices_list: list,
                    attempts: int, timedelta: int):
    """Arms camera if nobody at home for number if <attempts>.
    Disarms camera when anybody returns home"""
    any_at_home = get_ip_checks_for_address_list(home_devices_list, attempts)
    blink = blink_connect(username, password)
    camera = blink_cameras_list(blink)(0)
    msg = (f'{time_now()}: Successfully connected to camera: {camera.name}. '
           f'{camera_status(camera.arm)}')
    print(msg)
    send_message(msg)

    while True:
        if not any([check() for check in any_at_home]):  # if nobody home
            if not camera.arm:
                arm_camera(camera, blink)
        else:
            if camera.arm:
                disarm_camera(camera, blink)
        sleep(timedelta)


if __name__ == '__main__':
    TIMEDELTA_CHECK = 60  # seconds between hosts alive check
    ATTEMPTS = 5  # Number of checks until person become counted absent
    SAFETY_WAIT_BLINK_API = 60  # Needed for safe calls to Blink API (seconds)

    load_dotenv()
    USERNAME = getenv('BLINK_CAMERA_AUTO_ARM_USERNAME')
    PASSWORD = getenv('BLINK_CAMERA_AUTO_ARM_PASSWORD')
    DEVICES = getenv('BLINK_CAMERA_AUTO_ARM_DEVICES')
    TELEGRAM_BOT_TOKEN = getenv('TELEGRAM_BOT_TOKEN')
    TELEGRAM_CHAT_ID = getenv('TELEGRAM_CHAT_ID')

    send_message = get_telegram_messenger(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

    if USERNAME and PASSWORD and DEVICES:
        DEVICES = list(map(str.strip, DEVICES.split(',')))
        start_main_loop(USERNAME, PASSWORD, DEVICES, ATTEMPTS, TIMEDELTA_CHECK)
    else:
        raise KeyError('Environment file does not contain required fields.')
