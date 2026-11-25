import asyncio
import logging
from datetime import datetime

from aiogram import Router, F, types, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, ReplyKeyboardRemove, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.data.other.accesses import TypeOfChats
from bot.data.repositories.AdminRepository import AdminRepository
from bot.data.repositories.BroadcastRepository import BroadcastRepository
from bot.data.repositories.ChatRepository import ChatRepository
from bot.domain.tools.MessageSpamTool import spam_all_groups, estimate_seconds
from bot.domain.tools.edit_helper import edit_or_answer
from bot.presentation.keyboard.admin_ import (
    MainMenuCallback, BroadcastCategoryCallback, BroadcastActionCallback, BroadcastHistoryCallback,
    kb_messaging_category, kb_cancel, kb_skip_cancel, kb_yes_skip_cancel, kb_send_cancel,
    kb_broadcast_menu, kb_broadcast_history, kb_broadcast_history_back, kb_back_to_main
)
from bot.domain.states.SendMessage import SendMessageState

router = Router()


@router.callback_query(MainMenuCallback.filter(F.action == "broadcast"))
async def show_broadcast_menu(callback: CallbackQuery):
    await edit_or_answer(callback, "📤 Рассылка:", reply_markup=kb_broadcast_menu())
    await callback.answer()


@router.callback_query(BroadcastActionCallback.filter(F.action == "new"))
async def start_create_message(callback: CallbackQuery, state: FSMContext):
    admin = await AdminRepository.is_admin(callback.from_user.id)
    await state.set_state(SendMessageState.category)
    await edit_or_answer(callback, "Выберите категорию группы:", reply_markup=kb_messaging_category(admin))
    await callback.answer()


@router.callback_query(BroadcastHistoryCallback.filter(F.action == "list"))
async def show_broadcast_history(callback: CallbackQuery):
    records = await BroadcastRepository.get_last_broadcasts(20) or []
    if not records:
        await edit_or_answer(callback, "История рассылок пуста.", reply_markup=kb_back_to_main())
    else:
        await edit_or_answer(
            callback,
            f"📜 Последние {len(records)} рассылок:",
            reply_markup=kb_broadcast_history(records)
        )
    await callback.answer()


@router.callback_query(BroadcastHistoryCallback.filter(F.action == "view"))
async def view_broadcast_record(callback: CallbackQuery, callback_data: BroadcastHistoryCallback):
    records = await BroadcastRepository.get_last_broadcasts(20) or []
    record = next((r for r in records if r.id == callback_data.record_id), None)
    if not record:
        await callback.answer("Запись не найдена", show_alert=True)
        return
    date_str = record.sent_at.strftime("%d.%m.%Y %H:%M")
    extras = []
    if record.has_photo:
        extras.append("фото/видео")
    if record.has_buttons:
        extras.append("кнопки")
    extras_text = " + " + ", ".join(extras) if extras else "только текст"
    text = (
        f"<b>Рассылка от {date_str}</b>\n\n"
        f"Админ: <b>{record.admin_name}</b> (<code>{record.admin_id}</code>)\n"
        f"Категория: <b>{record.category}</b>\n"
        f"Отправлено в: <b>{record.groups_count}</b> групп\n"
        f"Медиа: {extras_text}\n\n"
        f"<b>Текст:</b>\n{record.message_text or '—'}"
    )
    await edit_or_answer(callback, text, reply_markup=kb_broadcast_history_back())
    await callback.answer()


@router.callback_query(BroadcastCategoryCallback.filter(), SendMessageState.category)
async def set_category(callback: CallbackQuery, callback_data: BroadcastCategoryCallback, state: FSMContext):
    await state.update_data(category=callback_data.category)
    await state.set_state(SendMessageState.message)
    await edit_or_answer(callback, "Введите текст для рассылки:", reply_markup=kb_cancel())
    await callback.answer()


@router.message(SendMessageState.message)
async def set_message(message: types.Message, state: FSMContext):
    await state.update_data(message=message.html_text, buttons=[])
    await state.set_state(SendMessageState.button)
    await message.answer("Хотите добавить кнопку?", reply_markup=kb_yes_skip_cancel())


@router.callback_query(BroadcastActionCallback.filter(F.action.in_(["yes", "skip"])), SendMessageState.button)
async def set_button(callback: CallbackQuery, callback_data: BroadcastActionCallback, state: FSMContext):
    if callback_data.action == "skip":
        await state.set_state(SendMessageState.photo)
        await edit_or_answer(
            callback,
            "Отправьте фото, видео или гифку (сжатый формат, не файл):",
            reply_markup=kb_skip_cancel()
        )
        await callback.answer()
        return

    await state.set_state(SendMessageState.buttonText)
    await _add_new_button(state)
    await edit_or_answer(
        callback,
        "Текст кнопки (до 50 символов) или пропустите:",
        reply_markup=kb_skip_cancel()
    )
    await callback.answer()


@router.message(SendMessageState.buttonText)
async def set_button_text(message: types.Message, state: FSMContext):
    if len(message.text) > 50:
        await message.answer("До 50 символов!", reply_markup=kb_skip_cancel())
        return
    await _set_text_last_button(state, message.text)
    await state.set_state(SendMessageState.buttonUrl)
    await message.answer("Отправьте URL, куда перенаправить:", reply_markup=kb_cancel())


@router.callback_query(BroadcastActionCallback.filter(F.action == "skip"), SendMessageState.buttonText)
async def skip_button_text(callback: CallbackQuery, state: FSMContext):
    await _set_text_last_button(state, "skip")
    await state.set_state(SendMessageState.buttonUrl)
    await edit_or_answer(callback, "Отправьте URL, куда перенаправить:", reply_markup=kb_cancel())
    await callback.answer()


@router.message(SendMessageState.buttonUrl)
async def set_button_url(message: types.Message, state: FSMContext):
    url = message.text.strip()
    if not url.startswith(("http://", "https://")):
        await message.answer(
            "URL должен начинаться с <code>http://</code> или <code>https://</code>",
            reply_markup=kb_cancel()
        )
        return
    await _set_url_last_button(state, url)
    await state.set_state(SendMessageState.buttonRepeat)
    await message.answer("Хотите добавить ещё кнопку?", reply_markup=kb_yes_skip_cancel())


@router.callback_query(BroadcastActionCallback.filter(F.action.in_(["yes", "skip"])), SendMessageState.buttonRepeat)
async def set_button_repeat(callback: CallbackQuery, callback_data: BroadcastActionCallback, state: FSMContext):
    data = await state.get_data()

    if len(data.get("buttons", [])) >= 10:
        await callback.answer("Уже 10 кнопок — это лимит", show_alert=True)

    if callback_data.action == "skip" or len(data.get("buttons", [])) >= 10:
        await state.set_state(SendMessageState.photo)
        await edit_or_answer(
            callback,
            "Отправьте фото, видео или гифку (сжатый формат, не файл):",
            reply_markup=kb_skip_cancel()
        )
        await callback.answer()
        return

    await state.set_state(SendMessageState.buttonText)
    await _add_new_button(state)
    await edit_or_answer(
        callback,
        "Текст кнопки (до 50 символов) или пропустите:",
        reply_markup=kb_skip_cancel()
    )
    await callback.answer()


@router.message(SendMessageState.photo, F.photo | F.animation | F.video)
async def set_photo(message: types.Message, state: FSMContext):
    await _show_preview(message, state)


@router.callback_query(BroadcastActionCallback.filter(F.action == "skip"), SendMessageState.photo)
async def skip_photo(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SendMessageState.preview)
    data = await state.get_data()
    kb = _build_buttons_markup(data)
    await callback.message.answer(data["message"], reply_markup=kb)
    await callback.message.answer("Всё окей?", reply_markup=kb_send_cancel())
    await callback.answer()


@router.callback_query(BroadcastActionCallback.filter(F.action == "send"), SendMessageState.preview)
async def send_broadcast(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    admin = await AdminRepository.is_admin(callback.from_user.id)
    await state.clear()

    category = data.get("category")
    if category == TypeOfChats.ALL.value:
        chats = await ChatRepository.all_chats() or []
    else:
        chats = await ChatRepository.chat_by_type(category) or []

    eta = estimate_seconds(len(chats))
    await edit_or_answer(
        callback,
        f"📤 Рассылка запущена\n\n"
        f"Групп: <b>{len(chats)}</b>\n"
        f"Ориентировочное время: <b>{eta}</b>",
        reply_markup=None
    )
    await callback.answer()

    asyncio.create_task(_run_broadcast(
        bot=callback.bot,
        chat_id=callback.from_user.id,
        data=data,
        admin_id=str(callback.from_user.id),
        admin_name=admin.name if admin else str(callback.from_user.id),
        category=category or "unknown",
        total=len(chats),
    ))


async def _run_broadcast(bot: Bot, chat_id: int, data: dict, admin_id: str, admin_name: str, category: str, total: int):
    try:
        count = await spam_all_groups(bot, data, category)
        await BroadcastRepository.add_broadcast(
            admin_id=admin_id,
            admin_name=admin_name,
            category=category,
            message_text=(data.get("message") or "")[:500],
            has_photo=bool(data.get("photo") or data.get("video") or data.get("animation")),
            has_buttons=bool(data.get("buttons")),
            groups_count=count,
        )
        await bot.send_message(
            chat_id=chat_id,
            text=f"✅ Рассылка завершена\n\nДоставлено: <b>{count}</b> / {total} групп",
            reply_markup=kb_back_to_main()
        )
    except Exception as e:
        logging.error(f"_run_broadcast: {e}")
        await bot.send_message(
            chat_id=chat_id,
            text="❌ Ошибка во время рассылки. Проверьте логи.",
            reply_markup=kb_back_to_main()
        )


# ─── helpers ──────────────────────────────────────────────────────────────────

async def _show_preview(message: types.Message, state: FSMContext):
    await state.set_state(SendMessageState.preview)
    data = await state.get_data()
    kb = _build_buttons_markup(data)

    if message.content_type == "photo":
        await state.update_data(photo=message.photo[-1].file_id)
        data = await state.get_data()
        await message.answer_photo(data["photo"], caption=data["message"], reply_markup=kb)
    elif message.content_type == "animation":
        await state.update_data(animation=message.document.file_id)
        data = await state.get_data()
        await message.answer_animation(data["animation"], caption=data["message"], reply_markup=kb)
    elif message.content_type == "video":
        await state.update_data(video=message.video.file_id)
        data = await state.get_data()
        await message.answer_video(data["video"], caption=data["message"], reply_markup=kb)

    await message.answer("Всё окей?", reply_markup=kb_send_cancel())


def _build_buttons_markup(data):
    buttons = data.get("buttons", [])
    if buttons:
        kb = InlineKeyboardBuilder()
        for button in buttons:
            kb.add(InlineKeyboardButton(text=button["btn_text"], url=button["btn_url"]))
        kb.adjust(1)
        return kb.as_markup()
    return ReplyKeyboardRemove()


async def _add_new_button(state: FSMContext):
    data = await state.get_data()
    buttons = data.get("buttons", [])
    buttons.append({})
    await state.update_data(buttons=buttons)


async def _set_text_last_button(state: FSMContext, text: str):
    data = await state.get_data()
    buttons = data.get("buttons", [{}])
    buttons[-1]["btn_text"] = "Перейти" if text == "skip" else text
    await state.update_data(buttons=buttons)


async def _set_url_last_button(state: FSMContext, url: str):
    data = await state.get_data()
    buttons = data.get("buttons", [{}])
    buttons[-1]["btn_url"] = url
    await state.update_data(buttons=buttons)
