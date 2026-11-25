from html import escape

from aiogram import Router, Bot, types
from aiogram.filters import Command

from bot.data.repositories.ChatRepository import ChatRepository
from bot.domain.filters.IsGroup import IsGropFilter
from bot.domain.middlewares.IsGroupAdmin import IsGroupAdmin
from bot.domain.routers.admin.group.menu import update_
from bot.domain.routers.events_ import register_chat

router = Router()
router.include_routers(
    update_.router
)

router.message.middleware(IsGroupAdmin())
router.callback_query.middleware(IsGroupAdmin())


@router.message(Command("start"), IsGropFilter())
async def start(message: types.Message, bot: Bot):
    title = escape(message.chat.title or str(message.chat.id))
    if await ChatRepository.get_chat(message.chat.id):
        await message.answer(f"Группа <b>{title}</b> уже есть в базе.")
    elif await register_chat(bot, message.chat.id, message.chat.title):
        await message.answer(
            f"✅ Группа <b>{title}</b> (<code>{message.chat.id}</code>) добавлена в базу!\n"
            f"Назначьте категории в боте: «📋 Просмотреть группы» → «⚙️ Настройки»."
        )
    else:
        await message.answer("❌ Ошибка при добавлении группы.")
