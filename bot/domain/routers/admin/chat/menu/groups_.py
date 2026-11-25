from datetime import datetime

from aiogram import Router, F, Bot
from aiogram.types import Message

from bot.data.repositories.ChatRepository import ChatRepository
from bot.presentation.keyboard.admin_ import kb_back_to_main

router = Router()


@router.message(F.forward_from_chat)
async def handle_forwarded_group(message: Message, bot: Bot):
    chat = message.forward_from_chat
    if chat.type not in ("group", "supergroup"):
        await message.answer(
            "Это не сообщение из группы. Перешли сообщение из нужной группы.",
            reply_markup=kb_back_to_main()
        )
        return

    existing = await ChatRepository.get_chat(chat.id)
    if existing:
        await message.answer(
            f"Группа <b>{chat.title}</b> уже есть в базе.",
            reply_markup=kb_back_to_main()
        )
        return

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
        if member.status not in ("member", "administrator"):
            await message.answer(
                f"Бот не является участником группы <b>{chat.title}</b>. "
                f"Сначала добавьте бота в группу.",
                reply_markup=kb_back_to_main()
            )
            return
    except Exception:
        await message.answer(
            f"Не удалось проверить группу <b>{chat.title}</b>. "
            f"Убедитесь, что бот добавлен туда.",
            reply_markup=kb_back_to_main()
        )
        return

    new_chat = await ChatRepository.add_chat(
        group_id=chat.id,
        title=chat.title,
        datetime=datetime.now()
    )
    if new_chat:
        try:
            chat_info = await bot.get_chat(chat.id)
            if chat_info.invite_link:
                await ChatRepository.update_chat_link(chat.id, chat_info.invite_link)
        except Exception:
            pass
        await message.answer(
            f"✅ Группа <b>{chat.title}</b> (<code>{chat.id}</code>) добавлена в базу!\n\n"
            f"Категории ещё не назначены. Откройте группу через «📋 Просмотреть группы» → "
            f"«⚙️ Настройки», чтобы включить нужные категории.",
            reply_markup=kb_back_to_main()
        )
    else:
        await message.answer(
            "❌ Ошибка при добавлении группы.",
            reply_markup=kb_back_to_main()
        )
