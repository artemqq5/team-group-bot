import logging
from datetime import datetime
from html import escape

from aiogram import Router, F, Bot
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.types import ChatMemberUpdated, Message

from bot.data.repositories.AdminRepository import AdminRepository
from bot.data.repositories.ChatRepository import ChatRepository

router = Router()
router.my_chat_member.filter(F.chat.type.in_({"group", "supergroup"}))


async def register_chat(bot: Bot, chat_id: int, title: str) -> bool:
    if not await ChatRepository.add_chat(group_id=chat_id, title=title, datetime=datetime.now()):
        return False
    try:
        chat_info = await bot.get_chat(chat_id)
        if chat_info.invite_link:
            await ChatRepository.update_chat_link(chat_id, chat_info.invite_link)
    except Exception as e:
        logging.warning(f"register_chat: failed to fetch invite link for {chat_id}: {e}")
    return True


@router.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def bot_added_to_group(event: ChatMemberUpdated, bot: Bot):
    chat = event.chat
    if not await AdminRepository.is_admin(event.from_user.id):
        logging.info(f"bot added to {chat.id} by non-admin {event.from_user.id}, skipped")
        return

    title = escape(chat.title or str(chat.id))
    if await ChatRepository.get_chat(chat.id):
        text = f"Група <b>{title}</b> вже є в базі."
    elif await register_chat(bot, chat.id, chat.title):
        logging.info(f"group auto-added: {chat.id} by {event.from_user.id}")
        text = (
            f"✅ Група <b>{title}</b> (<code>{chat.id}</code>) додана до бази!\n\n"
            f"Категорії ще не призначені. Відкрийте групу через «📋 Переглянути групи» → "
            f"«⚙️ Налаштування», щоб увімкнути потрібні категорії."
        )
    else:
        text = f"❌ Помилка при додаванні групи <b>{title}</b>."

    try:
        await bot.send_message(event.from_user.id, text)
    except Exception as e:
        logging.warning(f"bot_added_to_group: failed to notify {event.from_user.id}: {e}")


@router.message(F.migrate_to_chat_id)
async def handle_migration(message: Message):
    updated = await ChatRepository.update_group_id(message.chat.id, message.migrate_to_chat_id)
    logging.info(f"group migration: {message.chat.id} → {message.migrate_to_chat_id}, updated={updated}")
