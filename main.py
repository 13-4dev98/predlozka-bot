import sys
import asyncio
import platform
import logging
import os
import re

from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from db_utils import init_db, is_user_blocked, block_user, unban_user

if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_FALLBACK_BOT_TOKEN")
ADMIN_IDS_STR = os.getenv("ADMIN_IDS")
MODERATION_GROUP_ID_STR = os.getenv("MODERATION_GROUP_ID")

ADMIN_IDS = []
TARGET_GROUP_ID = None

if ADMIN_IDS_STR:
    try:
        raw_ids = [admin_id.strip() for admin_id in ADMIN_IDS_STR.split(',')]
        ADMIN_IDS = [int(admin_id) for admin_id in raw_ids if admin_id]
    except ValueError:
        logging.critical(f"Invalid ADMIN_IDS format: '{ADMIN_IDS_STR}'. Please use comma-separated integers.")
        ADMIN_IDS = []
else:
    logging.warning("ADMIN_IDS environment variable is not set or empty.")

if not ADMIN_IDS:
     logging.critical("ADMIN_IDS list is empty after processing! Bot will not recognize any admins for actions.")

if MODERATION_GROUP_ID_STR:
    try:
        TARGET_GROUP_ID = int(MODERATION_GROUP_ID_STR)
        logging.info(f"Moderation group ID set to: {TARGET_GROUP_ID}")
    except ValueError:
        logging.critical(f"Invalid MODERATION_GROUP_ID format: '{MODERATION_GROUP_ID_STR}'. It must be an integer.")
        TARGET_GROUP_ID = None
else:
    logging.critical("MODERATION_GROUP_ID environment variable is not set. Suggestions cannot be forwarded.")
    TARGET_GROUP_ID = None

RENDER_EXTERNAL_URL = os.getenv('RENDER_EXTERNAL_URL')
BASE_WEBHOOK_URL = RENDER_EXTERNAL_URL if RENDER_EXTERNAL_URL else "YOUR_FALLBACK_HTTPS_URL"

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "your_strong_secret_here")
WEBHOOK_URL = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}"

WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.getenv('PORT', 8080))

router = Router()

def extract_user_id_from_prompt(text: str) -> int | None:
    match = re.search(r"User ID: <code>(\d+)</code>", text)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None

@router.message(CommandStart())
async def handle_start(message: Message):
    user_id = message.from_user.id
    if await is_user_blocked(user_id):
        await message.answer("Sorry, you have been blocked and cannot send suggestions.")
        return

    if user_id in ADMIN_IDS:
        try:
            await message.answer(
                "Hello, Admin! 👋\n"
                "You can manage suggestions in the moderation group.\n"
                "Use /unban <code>user_id</code> to unblock a user (in PM with the bot)."
            )
        except TelegramBadRequest as e:
             logging.error(f"Failed to send start message to admin {user_id} with HTML: {e}")
             await message.answer(
                 "Hello, Admin! You can manage suggestions in the moderation group. Use /unban user_id to unblock a user (in PM with the bot)."
             )
    else:
        await message.answer(
            "Hello! 👋\n"
            "Send your suggestion as text. You can also attach one screenshot (send a photo with a caption)."
        )

@router.message(F.text | F.photo, StateFilter(None))
async def handle_suggestion(message: Message, bot: Bot):
    user = message.from_user
    user_id = user.id

    if user_id in ADMIN_IDS:
        logging.debug(f"Ignoring suggestion handler for admin {user_id} (no state)")
        return

    if await is_user_blocked(user_id):
        await message.answer("Sorry, you have been blocked and cannot send suggestions.")
        return

    if not TARGET_GROUP_ID:
        logging.error("Cannot forward suggestion: MODERATION_GROUP_ID is not configured.")
        await message.reply("Sorry, there was an error sending your suggestion (admin configuration issue). Please try again later.")
        return

    user_info = f"📝 New suggestion from: {user.full_name}"
    if user.username:
        user_info += f" (@{user.username})"
    user_info += f"\nID: <code>{user_id}</code>"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Reply", callback_data=f"reply_{user_id}"),
            InlineKeyboardButton(text="🚫 Block", callback_data=f"block_{user_id}")
        ]
    ])

    sent_to_group = False
    try:
        if message.photo:
            await bot.copy_message(
                chat_id=TARGET_GROUP_ID,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                caption=f"{user_info}\n\n{message.caption or ''}",
                reply_markup=keyboard,
            )
        elif message.text:
            await bot.send_message(
                chat_id=TARGET_GROUP_ID,
                text=f"{user_info}\n\n{message.text}",
                reply_markup=keyboard,
            )
        sent_to_group = True
        logging.info(f"Suggestion from {user_id} forwarded to group {TARGET_GROUP_ID}")

    except TelegramAPIError as e:
        logging.error(f"Failed to send message to group {TARGET_GROUP_ID}: {e}")
        if "chat not found" in str(e).lower() or "bot was kicked" in str(e).lower():
             await message.reply("Sorry, could not send your suggestion. There might be an issue with the moderation group.")
        else:
             await message.reply("Sorry, a technical error occurred while sending your suggestion. Please try again later.")
    except Exception as e:
        logging.exception(f"Unknown error sending to group {TARGET_GROUP_ID}: {e}")
        await message.reply("Sorry, an unexpected error occurred while sending your suggestion.")

    if sent_to_group:
        await message.reply("Thank you! Your suggestion has been sent to the administration.")


@router.callback_query(F.data.startswith("reply_") | F.data.startswith("block_"))
async def handle_admin_action(callback: CallbackQuery, bot: Bot):
    admin_user = callback.from_user
    admin_id = admin_user.id

    if admin_id not in ADMIN_IDS:
        await callback.answer("This action is only available to administrators.", show_alert=True)
        return

    if callback.message and callback.message.chat.id != TARGET_GROUP_ID:
         await callback.answer("Please perform this action in the moderation group.", show_alert=True)
         logging.warning(f"Admin {admin_id} tried action '{callback.data}' outside moderation group ({callback.message.chat.id})")
         return

    action, user_id_str = callback.data.split("_", 1)
    try:
        user_id = int(user_id_str)
    except ValueError:
        await callback.answer("Error: Invalid user ID.", show_alert=True)
        return

    logging.info(f"[Admin Action] Admin: {admin_id}, Action: {action}, Target User: {user_id} in Group: {TARGET_GROUP_ID}")

    if action == "block":
        await block_user(user_id)
        logging.info(f"Admin {admin_id} initiated block for user {user_id}")
        await callback.answer(f"User {user_id} blocked.", show_alert=True)

        try:
            if callback.message:
                original_content = callback.message.text or callback.message.caption or f"Suggestion from ID: <code>{user_id}</code>"
                original_content = original_content.split('\n\n---')[0]

                new_text = f"{original_content}\n\n---\n🚫 User blocked by Admin {callback.from_user.full_name}"
                unban_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🟢 Unban User", callback_data=f"unban_{user_id}")]
                ])

                if callback.message.photo:
                     await bot.edit_message_caption(
                         chat_id=TARGET_GROUP_ID,
                         message_id=callback.message.message_id,
                         caption=new_text,
                         reply_markup=unban_keyboard
                     )
                else:
                    await callback.message.edit_text(
                        chat_id=TARGET_GROUP_ID,
                        message_id=callback.message.message_id,
                        text=new_text,
                        reply_markup=unban_keyboard
                    )
            else:
                 logging.warning("Could not get callback.message to edit after blocking.")
        except TelegramAPIError as e:
             logging.warning(f"Could not edit message in group {TARGET_GROUP_ID} after blocking user {user_id}: {e}")

    elif action == "reply":
        prompt_text = (
            f"{admin_user.mention_html()}, please send your reply for User ID: <code>{user_id}</code> "
            f"**by replying to this message**."
        )
        prompt_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel Reply", callback_data=f"cancel_prompt_{user_id}_{admin_id}")]
        ])
        try:
            await bot.send_message(
                chat_id=TARGET_GROUP_ID,
                text=prompt_text,
                reply_markup=prompt_keyboard
            )
            await callback.answer("Please reply to the new message sent in the group.")
            logging.info(f"Sent reply prompt to group {TARGET_GROUP_ID} for admin {admin_id} replying to {user_id}")
        except TelegramAPIError as e:
            logging.error(f"Failed to send reply prompt to group {TARGET_GROUP_ID}: {e}")
            await callback.answer("Error: Could not send reply prompt.", show_alert=True)


@router.callback_query(F.data.startswith("cancel_prompt_"))
async def handle_cancel_prompt(callback: CallbackQuery, bot: Bot):
    try:
        parts = callback.data.split("_")
        target_user_id = int(parts[2])
        prompting_admin_id = int(parts[3])
    except (IndexError, ValueError):
        await callback.answer("Error processing cancel request.", show_alert=True)
        logging.error(f"Could not parse cancel_prompt callback data: {callback.data}")
        return

    if callback.from_user.id != prompting_admin_id:
        await callback.answer("Only the admin who initiated the reply can cancel it.", show_alert=True)
        return

    try:
        await bot.delete_message(chat_id=callback.message.chat.id, message_id=callback.message.message_id)
        await callback.answer("Reply cancelled.")
        logging.info(f"Admin {callback.from_user.id} cancelled reply prompt for user {target_user_id}")
    except TelegramAPIError as e:
        logging.error(f"Failed to delete reply prompt message {callback.message.message_id}: {e}")
        await callback.answer("Could not delete the prompt message, but reply is cancelled.", show_alert=True)


@router.message(
    F.chat.id == TARGET_GROUP_ID,
    F.from_user.id.in_(ADMIN_IDS),
    F.reply_to_message,
    F.reply_to_message.from_user.is_bot,
    F.text
)
async def handle_group_reply_to_bot(message: Message, bot: Bot):
    bot_info = await bot.get_me()
    if message.reply_to_message.from_user.id != bot_info.id:
         return

    prompt_text = message.reply_to_message.text
    admin_reply_text = message.text
    admin_user = message.from_user

    target_user_id = extract_user_id_from_prompt(prompt_text)

    if target_user_id is None:
        logging.warning(f"Admin {admin_user.id} replied to bot message in group {TARGET_GROUP_ID}, but target_user_id not found in prompt: '{prompt_text}'")
        return

    if admin_user.mention_html() not in prompt_text and (admin_user.username and f"@{admin_user.username}" not in prompt_text):
         logging.warning(f"Admin {admin_user.id} replied to prompt intended for another admin. Prompt: '{prompt_text}'. Ignoring.")
         await message.reply("This reply prompt wasn't for you.", disable_notification=True)
         return

    logging.info(f"Processing group reply from Admin {admin_user.id} to User {target_user_id}")

    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=f"ℹ️ Reply from Administration:\n\n{admin_reply_text}"
        )
        logging.info(f"[Group Reply] SUCCESS: Admin {admin_user.id} replied to user {target_user_id}")

        try:
            await message.reply(f"✅ Reply sent to user <code>{target_user_id}</code>.", disable_notification=True)
            await bot.delete_message(
                 chat_id=message.chat.id,
                 message_id=message.reply_to_message.message_id
            )
        except TelegramAPIError as cleanup_err:
             logging.warning(f"Error during cleanup after group reply by {admin_user.id}: {cleanup_err}")

    except TelegramAPIError as e:
        logging.error(f"[Group Reply] FAILED to send reply to user {target_user_id}: {e}")
        await message.reply(
            f"⚠️ Could not send reply to user <code>{target_user_id}</code>. "
            f"They might have blocked the bot, or the ID might be invalid."
            f"\nError details: {e}"
        )
    except Exception as e:
        logging.exception(f"[Group Reply] UNKNOWN error sending reply to user {target_user_id}")
        await message.reply("An unexpected error occurred while sending the reply.")


@router.callback_query(F.data.startswith("unban_"))
async def handle_unban_button(callback: CallbackQuery, bot: Bot):
    admin_id = callback.from_user.id

    if admin_id not in ADMIN_IDS:
        await callback.answer("This action is only available to administrators.", show_alert=True)
        return

    if callback.message and callback.message.chat.id != TARGET_GROUP_ID:
         await callback.answer("Please perform this action in the moderation group.", show_alert=True)
         return

    _, user_id_str = callback.data.split("_", 1)
    try:
        user_id = int(user_id_str)
    except ValueError:
        await callback.answer("Error: Invalid user ID.", show_alert=True)
        return

    logging.info(f"[Unban Action] Admin: {admin_id} initiated unban via button for User: {user_id} in Group: {TARGET_GROUP_ID}")

    if await unban_user(user_id):
        await callback.answer(f"User {user_id} has been unblocked.", show_alert=True)
        try:
            if callback.message:
                 original_content = (callback.message.text or callback.message.caption or "").split("\n\n---")[0]
                 new_text = f"{original_content}\n\n---\n🟢 User unblocked by Admin {callback.from_user.full_name}"
                 keyboard = InlineKeyboardMarkup(inline_keyboard=[
                     [
                         InlineKeyboardButton(text="✅ Reply", callback_data=f"reply_{user_id}"),
                         InlineKeyboardButton(text="🚫 Block", callback_data=f"block_{user_id}")
                     ]
                 ])

                 if callback.message.photo:
                     await bot.edit_message_caption(
                         chat_id=TARGET_GROUP_ID,
                         message_id=callback.message.message_id,
                         caption=new_text,
                         reply_markup=keyboard
                     )
                 else:
                    await callback.message.edit_text(
                        chat_id=TARGET_GROUP_ID,
                        message_id=callback.message.message_id,
                        text=new_text,
                        reply_markup=keyboard
                    )
        except TelegramAPIError as e:
            logging.warning(f"Could not edit message in group {TARGET_GROUP_ID} after unblocking user {user_id} via button: {e}")
    else:
        await callback.answer(f"User {user_id} was not found in the block list or could not be unblocked.", show_alert=True)


@router.message(Command("unban"))
async def handle_unban_command(message: Message):
    admin_id = message.from_user.id
    if admin_id not in ADMIN_IDS:
        return

    if message.chat.id != admin_id:
        await message.reply("Please use the /unban command in a private chat with me.")
        return

    command_parts = message.text.split()
    if len(command_parts) != 2:
        await message.reply("Usage: /unban <code>user_id</code>")
        return

    try:
        user_id_to_unban = int(command_parts[1])
    except ValueError:
        await message.reply("Invalid User ID. Please provide a number.")
        return

    logging.info(f"[Unban Command] Admin: {admin_id} trying to unban User: {user_id_to_unban} via command in PM")

    if await unban_user(user_id_to_unban):
        await message.reply(f"✅ User <code>{user_id_to_unban}</code> has been unblocked.")
    else:
        await message.reply(f"⚠️ User <code>{user_id_to_unban}</code> was not found in the block list or an error occurred.")

async def on_startup(bot: Bot):
    if not BASE_WEBHOOK_URL or BASE_WEBHOOK_URL == "YOUR_FALLBACK_HTTPS_URL":
        logging.warning("Skipping webhook setup because BASE_WEBHOOK_URL is not configured correctly.")
        return

    logging.info(f"Attempting to set/reset webhook to {WEBHOOK_URL}")
    try:
        await bot.set_webhook(
            url=WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True,
            allowed_updates=['message', 'callback_query']
        )
        logging.info(f"Webhook set/reset successfully attempted for {WEBHOOK_URL}")

        if TARGET_GROUP_ID:
            try:
                 await bot.send_message(TARGET_GROUP_ID, "🤖 Bot started and ready to receive suggestions.")
            except Exception as e:
                 logging.warning(f"Could not send startup message to group {TARGET_GROUP_ID}: {e}")

    except Exception as e:
        logging.error(f"Failed to set/reset webhook {WEBHOOK_URL}: {e}")

async def on_shutdown(bot: Bot):
    logging.info("Shutting down.. removing webhook")
    if TARGET_GROUP_ID:
        try:
             await bot.send_message(TARGET_GROUP_ID, "💤 Bot is stopping...")
        except Exception as e:
             logging.warning(f"Could not send shutdown message to group {TARGET_GROUP_ID}: {e}")

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        logging.info("Webhook removed.")
    except Exception as e:
        logging.error(f"Failed to delete webhook: {e}")
    await bot.session.close()
    logging.info("Bot session closed.")


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )
    logging.info("Starting bot in webhook mode...")

    if not BOT_TOKEN or BOT_TOKEN == "YOUR_FALLBACK_BOT_TOKEN":
       logging.critical("BOT_TOKEN environment variable not set or is fallback!")
       sys.exit(1)
    if not TARGET_GROUP_ID:
        logging.critical("MODERATION_GROUP_ID is not configured. Bot cannot forward suggestions.")
        # sys.exit(1) # Uncomment if group is mandatory
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS is not configured. No one can manage suggestions.")
        # sys.exit(1) # Uncomment if admins are mandatory
    if not BASE_WEBHOOK_URL or BASE_WEBHOOK_URL == "YOUR_FALLBACK_HTTPS_URL":
        logging.critical("RENDER_EXTERNAL_URL environment variable not found or fallback URL not set!")
        logging.warning("Cannot set webhook without a valid BASE_WEBHOOK_URL. Bot might not receive updates if webhook isn't set manually.")
        # sys.exit(1) # Uncomment if webhook is mandatory

    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()
    dp.include_router(router)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)

    logging.info(f"Starting web server on {WEB_SERVER_HOST}:{WEB_SERVER_PORT} for webhook path {WEBHOOK_PATH}")
    await site.start()

    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped by user or system signal.")
    except OSError as e:
         logging.critical(f"Could not start web server (maybe port {WEB_SERVER_PORT} is busy?): {e}")
         sys.exit(1)
    except Exception as e:
        logging.critical(f"Unhandled exception during startup or main loop: {e}", exc_info=True)
        sys.exit(1)
