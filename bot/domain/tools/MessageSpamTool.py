import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.data.other.accesses import TypeOfChats
from bot.data.repositories.AdminRepository import AdminRepository
from bot.data.repositories.ChatRepository import ChatRepository


async def spam_all_groups(bot: Bot, data: dict, chat_type: str | None = None) -> int:
    if chat_type is None or chat_type == TypeOfChats.ALL.value:
        chats = await ChatRepository.all_chats() or []
    else:
        chats = await ChatRepository.chat_by_type(chat_type) or []

    counter = 0
    for chat in chats:
        try:
            await _send_message(bot, data, chat.group_id)
            counter += 1
        except TelegramRetryAfter as e:
            logging.warning(f"spam: flood control {chat.group_id}, waiting {e.retry_after}s")
            await asyncio.sleep(e.retry_after)
            try:
                await _send_message(bot, data, chat.group_id)
                counter += 1
            except Exception as ex:
                logging.error(f"spam: retry failed ({chat.group_id}): {ex}")
        except Exception as e:
            logging.error(f"spam: send failed ({chat.group_id}): {e}")
        await asyncio.sleep(0.05)

    return counter


def estimate_seconds(groups_count: int) -> str:
    secs = max(1, round(groups_count * 0.05))
    if secs < 60:
        return f"~{secs} сек"
    minutes = secs // 60
    remainder = secs % 60
    return f"~{minutes} хв {remainder} сек" if remainder else f"~{minutes} хв"


async def _send_message(bot: Bot, data: dict, group_id):
    buttons = data.get("buttons", [])
    if buttons:
        kb = InlineKeyboardBuilder()
        for button in buttons:
            kb.add(InlineKeyboardButton(text=button["btn_text"], url=button["btn_url"]))
        kb.adjust(1)
        markup = kb.as_markup()
    else:
        markup = ReplyKeyboardRemove()

    if data.get("photo"):
        await bot.send_photo(chat_id=group_id, photo=data["photo"], caption=data["message"], reply_markup=markup)
    elif data.get("video"):
        await bot.send_video(chat_id=group_id, video=data["video"], caption=data["message"], reply_markup=markup)
    elif data.get("animation"):
        await bot.send_animation(chat_id=group_id, animation=data["animation"], caption=data["message"], reply_markup=markup)
    else:
        await bot.send_message(chat_id=group_id, text=data["message"], reply_markup=markup)


async def push_new_user_added(bot: Bot, message: str):
    admins = await AdminRepository.get_admins() or []
    for admin in admins:
        try:
            await bot.send_message(chat_id=admin.telegram_id, text=message)
        except Exception as e:
            logging.error(f"push_new_user_added: failed to notify admin {admin.telegram_id}: {e}")
