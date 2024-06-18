import asyncio
import time

from globals import Globals
from telegram import Bot, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackContext,
)


class BlinkTelegramBot:
    """Telegram bot for online camera application management."""

    BOT_INVOCATION_COMMAND = 'camerabot'

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._bot = Bot(token=self.bot_token)
        self.start_listener_timeout = 10

    async def start_listener_task(
        self, bot_token: None | str = None, chat_id: None | str = None
    ) -> None:
        """Listener asyncio task for telegram handlers."""
        if bot_token is None:
            bot_token = self.bot_token
        if chat_id is None:
            chat_id = self.chat_id

        application = Application.builder().token(bot_token).build()

        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message)
        )

        application.add_handler(
            CommandHandler(self.BOT_INVOCATION_COMMAND, self.handle_commands)
        )

        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        await application.bot.send_message(
            chat_id=chat_id,
            text="Bot is started.",
        )
        while True:
            await asyncio.sleep(self.start_listener_timeout)

    def send_message(self, message: str) -> None:
        """Send message to telegram chat."""
        event_loop = asyncio.get_event_loop()
        asyncio.ensure_future(
            self._bot.send_message(text=message, chat_id=self.chat_id),
            loop=event_loop,
        )

    @staticmethod
    async def handle_message(update: Update, context: CallbackContext):
        """Handle incoming Telegram messages."""
        if not Globals.is_auth_key_required:
            return
        text = update.message.text
        if text:
            Globals.auth_key = text

    @staticmethod
    async def handle_commands(update: Update, context: CallbackContext):
        """Handle incoming Telegram commands."""

        async def display_help() -> None:
            help_message = 'Commands:\nstatus\nenable\ndisable.'
            await context.bot.send_message(chat_id=chat_id, text=help_message)

        def get_time_status_changed() -> str:
            if not Globals.time_of_last_status_change:
                return 'Not changed'
            elapsed_time = time.time() - Globals.time_of_last_status_change
            days, remainder = divmod(elapsed_time, 86400)
            hours, remainder = divmod(remainder, 3600)
            minutes, _ = divmod(remainder, 60)
            return f'{int(days):02d}d {int(hours):02d}h {int(minutes):02d}m'

        chat_id = update.effective_chat.id
        args = context.args

        if not args:
            await display_help()
            return

        message = None
        subcommand = ' '.join(args).strip()
        match subcommand:
            case 'disable':
                Globals.is_app_disabled = True
                message = f'Auto arming application disabled.'
            case 'enable':
                Globals.is_app_disabled = False
                message = f'Auto arming application enabled.'
            case 'status':
                while Globals.is_status_sync_requested:
                    await asyncio.sleep(5)
                message = (
                    f'Application status: '
                    f'{"disabled" if Globals.is_app_disabled else "enabled"}.\n'
                    f'Camera is: '
                    f'{"armed" if Globals.is_camera_armed else "disarmed"}.\n'
                    f'Status changed: {get_time_status_changed()}\n\n'
                )
                for ip, status in Globals.ips_status.items():
                    message += f'[{ip}]: {"online" if status else "offline"}\n'

        if message:
            await context.bot.send_message(chat_id=chat_id, text=message)
