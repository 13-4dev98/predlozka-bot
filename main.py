import sys
import asyncio
import platform
import logging
import os

from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, User
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

class AdminReply(StatesGroup):
    awaiting_group_reply = State()

router = Router()

def get_admin_mention(user: User) -> str:
    if user.username:
        return f"@{user.username}"
    else:
        return user.mention_html(user.full_name)

async def delete_message_safe(bot: Bot, chat_id: int, message_id: int, log_tag: str = ""):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        logging.info(f"[{log_tag}] Deleted message {message_id} in chat {chat_id}")
    except TelegramAPIError as e:
        logging.warning(f"[{log_tag}] Could not delete message {message_id} in chat {chat_id}: {e}")

async def edit_original_message_status(bot: Bot, chat_id: int, message_id: int, status_text: str, target_user_id: int):
    try:
        original_msg = await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=None
        )

        original_content = original_msg.text or original_msg.caption or f"Suggestion from ID: <code>{target_user_id}</code>"
        original_content = original_content.split('\n\n---')[0]

        new_text = f"{original_content}\n\n---\n{status_text}"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                 InlineKeyboardButton(text="✅ Reply", callback_data=f"reply_{target_user_id}"),
                 InlineKeyboardButton(text="🚫 Block", callback_data=f"block_{target_user_id}")
            ]
        ])

        if original_msg.photo:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=new_text,
                reply_markup=keyboard
            )
        else:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=new_text,
                reply_markup=keyboard
            )
        logging.info(f"Updated original message {message_id} status in group {chat_id}")

    except TelegramAPIError as e:
        logging.warning(f"Could not edit original message {message_id} in group {chat_id} status: {e}")


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
                "Use /unban <code>user_id</code> to unblock a user."
            )
        except TelegramBadRequest as e:
             logging.error(f"Failed to send start message to admin {user_id} with HTML: {e}")
             await message.answer(
                 "Hello, Admin! You can manage suggestions in the moderation group. Use /unban user_id to unblock a user."
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
async def handle_admin_action(callback: CallbackQuery, state: FSMContext, bot: Bot):
    admin_id = callback.from_user.id
    admin_user = callback.from_user

    if admin_id not in ADMIN_IDS:
        await callback.answer("This action is only available to administrators.", show_alert=True)
        return

    if not callback.message or not callback.message.chat.id == TARGET_GROUP_ID:
         await callback.answer("This action must be performed in the moderation group.", show_alert=True)
         logging.warning(f"Admin {admin_id} tried action outside target group {TARGET_GROUP_ID}")
         return

    action, user_id_str = callback.data.split("_", 1)
    try:
        user_id = int(user_id_str)
    except ValueError:
        await callback.answer("Error: Invalid user ID.", show_alert=True)
        return

    group_chat_id = callback.message.chat.id
    original_message_id = callback.message.message_id

    logging.info(f"[Admin Action] Admin: {admin_id}, Action: {action}, Target User: {user_id} in Group: {group_chat_id}, Msg: {original_message_id}")

    if action == "block":
        await block_user(user_id)
        logging.info(f"Admin {admin_id} initiated block for user {user_id}")
        await callback.answer(f"User {user_id} blocked.", show_alert=True)

        try:
            if callback.message:
                original_content = callback.message.text or callback.message.caption or f"Suggestion from ID: <code>{user_id}</code>"
                original_content = original_content.split('\n\n---')[0]

                new_text = f"{original_content}\n\n---\n🚫 User blocked by Admin {get_admin_mention(admin_user)}"
                unban_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🟢 Unban User", callback_data=f"unban_{user_id}")]
                ])

                if callback.message.photo:
                     await bot.edit_message_caption(
                         chat_id=group_chat_id,
                         message_id=original_message_id,
                         caption=new_text,
                         reply_markup=unban_keyboard
                     )
                else:
                    await callback.message.edit_text(
                        chat_id=group_chat_id,
                        message_id=original_message_id,
                        text=new_text,
                        reply_markup=unban_keyboard
                    )
            else:
                 logging.warning("Could not get callback.message to edit after blocking.")
        except TelegramAPIError as e:
             logging.warning(f"Could not edit message in group {group_chat_id} after blocking user {user_id}: {e}")

    elif action == "reply":
        admin_mention = get_admin_mention(admin_user)
        prompt_text = f"{admin_mention}, please send your reply for user ID <code>{user_id}</code> in this chat.\nSend /cancel to abort."
        try:
            prompt_message = await bot.send_message(
                chat_id=group_chat_id,
                text=prompt_text
            )
            await state.set_state(AdminReply.awaiting_group_reply)
            await state.update_data(
                target_user_id=user_id,
                group_chat_id=group_chat_id,
                prompt_message_id=prompt_message.message_id,
                original_message_id=original_message_id,
                admin_id=admin_id
            )
            logging.info(f"[Admin Action] State set to AdminReply.awaiting_group_reply for admin {admin_id}, target {user_id}")

            await callback.answer("Please send your reply in this chat.")

        except TelegramAPIError as e:
            logging.error(f"Failed to send reply prompt to group {group_chat_id}: {e}")
            await callback.answer("Error: Could not send reply prompt.", show_alert=True)
            await state.clear()

@router.callback_query(F.data.startswith("unban_"))
async def handle_unban_button(callback: CallbackQuery, bot: Bot):
    admin_id = callback.from_user.id
    admin_user = callback.from_user

    if admin_id not in ADMIN_IDS:
        await callback.answer("This action is only available to administrators.", show_alert=True)
        return

    if not callback.message or not callback.message.chat.id == TARGET_GROUP_ID:
         await callback.answer("This action must be performed in the moderation group.", show_alert=True)
         logging.warning(f"Admin {admin_id} tried unban outside target group {TARGET_GROUP_ID}")
         return

    _, user_id_str = callback.data.split("_", 1)
    try:
        user_id = int(user_id_str)
    except ValueError:
        await callback.answer("Error: Invalid user ID.", show_alert=True)
        return

    group_chat_id = callback.message.chat.id
    original_message_id = callback.message.message_id

    logging.info(f"[Unban Action] Admin: {admin_id} initiated unban via button for User: {user_id} in Group: {group_chat_id}")

    if await unban_user(user_id):
        await callback.answer(f"User {user_id} has been unblocked.", show_alert=True)
        try:
            if callback.message:
                 original_content = (callback.message.text or callback.message.caption or "").split("\n\n---")[0]
                 new_text = f"{original_content}\n\n---\n🟢 User unblocked by Admin {get_admin_mention(admin_user)}"
                 keyboard = InlineKeyboardMarkup(inline_keyboard=[
                     [
                         InlineKeyboardButton(text="✅ Reply", callback_data=f"reply_{user_id}"),
                         InlineKeyboardButton(text="🚫 Block", callback_data=f"block_{user_id}")
                     ]
                 ])

                 if callback.message.photo:
                     await bot.edit_message_caption(
                         chat_id=group_chat_id,
                         message_id=original_message_id,
                         caption=new_text,
                         reply_markup=keyboard
                     )
                 else:
                    await callback.message.edit_text(
                        chat_id=group_chat_id,
                        message_id=original_message_id,
                        text=new_text,
                        reply_markup=keyboard
                    )
        except TelegramAPIError as e:
            logging.warning(f"Could not edit message in group {group_chat_id} after unblocking user {user_id} via button: {e}")
    else:
        await callback.answer(f"User {user_id} was not found in the block list or could not be unblocked.", show_alert=True)

@router.message(Command("cancel"), AdminReply.awaiting_group_reply)
async def cancel_reply_state(message: Message, state: FSMContext, bot: Bot):
    admin_id = message.from_user.id
    current_chat_id = message.chat.id

    data = await state.get_data()
    expected_group_chat_id = data.get('group_chat_id')
    prompt_message_id = data.get('prompt_message_id')
    state_admin_id = data.get('admin_id')
    target_user_id = data.get('target_user_id', 'unknown')
    original_message_id = data.get('original_message_id')

    logging.info(f"Cancel cmd from admin {admin_id} in chat {current_chat_id}. State admin: {state_admin_id}, state chat: {expected_group_chat_id}")

    if admin_id != state_admin_id or current_chat_id != expected_group_chat_id:
         logging.warning(f"Cancel command received from wrong admin/chat. Ignoring.")
         # Optionally notify user they aren't the one replying or are in wrong chat
         await delete_message_safe(bot, current_chat_id, message.message_id, "CancelWrongCtx")
         return

    logging.info(f"Cancelling state AdminReply.awaiting_group_reply for admin {admin_id}. State data: {data}")
    await state.clear()

    await delete_message_safe(bot, expected_group_chat_id, prompt_message_id, "CancelCleanup")
    await delete_message_safe(bot, current_chat_id, message.message_id, "CancelCleanup")

    try:
        await bot.send_message(
            chat_id=current_chat_id,
            text=f"Reply action for user <code>{target_user_id}</code> cancelled by {get_admin_mention(message.from_user)}."
        )
    except TelegramAPIError as e:
        logging.warning(f"Could not send cancel confirmation to group {current_chat_id}: {e}")

    if original_message_id and expected_group_chat_id:
         await edit_original_message_status(
             bot, expected_group_chat_id, original_message_id,
             f"❌ Reply cancelled by Admin {get_admin_mention(message.from_user)}",
             target_user_id if isinstance(target_user_id, int) else 0
         )


@router.message(AdminReply.awaiting_group_reply, F.text)
async def process_admin_group_reply(message: Message, state: FSMContext, bot: Bot):
    admin_id = message.from_user.id
    admin_user = message.from_user
    current_chat_id = message.chat.id
    admin_reply_text = message.text

    data = await state.get_data()
    target_user_id = data.get('target_user_id')
    expected_group_chat_id = data.get('group_chat_id')
    prompt_message_id = data.get('prompt_message_id')
    original_message_id = data.get('original_message_id')
    state_admin_id = data.get('admin_id')

    logging.info(f"[Process Group Reply] Msg from {admin_id} in {current_chat_id}. State admin: {state_admin_id}, State chat: {expected_group_chat_id}")

    if not all([target_user_id, expected_group_chat_id, prompt_message_id, original_message_id, state_admin_id]):
        logging.warning(f"[Process Group Reply] Incomplete state data for admin {admin_id}. Clearing state.")
        await state.clear()
        return

    if current_chat_id != expected_group_chat_id or admin_id != state_admin_id:
        logging.warning(f"[Process Group Reply] Message from wrong admin/chat. Ignoring. MsgAdmin: {admin_id}, StateAdmin: {state_admin_id}, MsgChat: {current_chat_id}, StateChat: {expected_group_chat_id}")
        return

    if admin_reply_text.startswith('/'):
         logging.debug(f"Ignoring command '{admin_reply_text}' in group reply handler.")
         return

    logging.info(f"[Process Group Reply] Processing reply from admin {admin_id} for user {target_user_id}")
    await state.clear()

    reply_sent_successfully = False
    status_text = ""
    try:
        await bot.send_message(
            chat_id=target_user_id,
            text=f"ℹ️ Reply from Administration:\n\n{admin_reply_text}"
        )
        logging.info(f"[Process Group Reply] SUCCESS: Admin {admin_id} replied to user {target_user_id}")
        reply_sent_successfully = True
        status_text = f"✅ Replied by Admin {get_admin_mention(admin_user)}"
    except TelegramAPIError as e:
        logging.error(f"[Process Group Reply] FAILED to send reply to user {target_user_id}: {e}")
        status_text = f"⚠️ Reply failed by Admin {get_admin_mention(admin_user)} (User unreachable?)"
        try:
            await bot.send_message(
                chat_id=current_chat_id,
                text=f"⚠️ {get_admin_mention(admin_user)}, could not send reply to user <code>{target_user_id}</code>. They might have blocked the bot.\nError: {e}",
                reply_to_message_id=message.message_id
            )
        except TelegramAPIError as notify_err:
             logging.error(f"Failed to notify admin about reply failure in group {current_chat_id}: {notify_err}")

    await delete_message_safe(bot, current_chat_id, prompt_message_id, "ReplyCleanup")
    await delete_message_safe(bot, current_chat_id, message.message_id, "ReplyCleanup")

    await edit_original_message_status(bot, current_chat_id, original_message_id, status_text, target_user_id)


@router.message(Command("unban"))
async def handle_unban_command(message: Message):
    admin_id = message.from_user.id
    if admin_id not in ADMIN_IDS:
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

    logging.info(f"[Unban Command] Admin: {admin_id} trying to unban User: {user_id_to_unban} via command")

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

async def ping_handler(request):
    return web.Response(text="OK", status=200)

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
        sys.exit(1)
    if not ADMIN_IDS:
        logging.warning("ADMIN_IDS is not configured. No one can manage suggestions.")
    if not BASE_WEBHOOK_URL or BASE_WEBHOOK_URL == "YOUR_FALLBACK_HTTPS_URL":
        logging.critical("RENDER_EXTERNAL_URL environment variable not found or fallback URL not set!")
        logging.warning("Cannot set webhook without a valid BASE_WEBHOOK_URL.")

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
    
    app.router.add_get('/ping', ping_handler)

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
