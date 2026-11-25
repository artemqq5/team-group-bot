import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.data.repositories.ChatRepository import ChatRepository

router = Router()


@router.message(
    Command("agency_fb", "agency_google", "apps", "shop_google", "shop_fb", "creo", "console", "affiliate_mp", "partner_mp", "media_mt", "media_mp", "partner_mt"))
async def update_chat(message: Message, state: FSMContext):
    chat_type = message.text.split(' ')[0].replace("/", "")
    chat_type = chat_type.split("@")[0] if "@" in chat_type else chat_type
    available = 0
    try:
        update_available = int(message.text.split(' ')[1])
        if update_available in (0, 1):
            available = update_available
    except Exception as e:
        logging.warning(f"update_chat: invalid argument — {e}")

    if await ChatRepository.update_chat_type(message.chat.id, chat_type, available):
        await message.answer(f"Группа обновила статус {chat_type} на {available}")
    else:
        await message.answer(f"Ошибка обновления статуса, возможно, вы меняете на тот же")
