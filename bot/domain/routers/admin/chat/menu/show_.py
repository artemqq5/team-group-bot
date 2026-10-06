from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.text_decorations import html_decoration as hd

from bot.data.other.accesses import access_admin_to_chat, TypeOfChats
from bot.data.repositories.AdminRepository import AdminRepository
from bot.data.repositories.ChatRepository import ChatRepository
from bot.data.repositories.UserRepository import UserRepository
from bot.domain.tools.edit_helper import edit_or_answer
from bot.presentation.keyboard.admin_ import (
    MainMenuCallback, ShowTypeCallback, GroupCallback, GroupPageCallback,
    GroupSettingsCallback, GroupToggleCallback, GroupDeleteCallback,
    kb_type_group, generate_pagination_groups, kb_group_info, kb_group_settings,
    kb_group_delete_confirm, kb_back_to_main, CATEGORY_LABELS
)

router = Router()


@router.callback_query(MainMenuCallback.filter(F.action == "groups"))
async def show_type_selection(callback: CallbackQuery):
    admin = await AdminRepository.is_admin(callback.from_user.id)
    await edit_or_answer(callback, "Оберіть тип групи:", reply_markup=kb_type_group(admin))
    await callback.answer()


@router.callback_query(ShowTypeCallback.filter())
async def show_groups_by_type(callback: CallbackQuery, callback_data: ShowTypeCallback, state: FSMContext):
    admin = await AdminRepository.is_admin(callback.from_user.id)
    chat_type = callback_data.chat_type

    if chat_type not in access_admin_to_chat.get(admin.role, []) and chat_type != "unspecified":
        await callback.answer("Немає доступу", show_alert=True)
        return

    if chat_type == "unspecified":
        chats = await ChatRepository.unspecified_chats()
        title = "Невизначені групи"
    elif chat_type == TypeOfChats.ALL.value:
        chats = await ChatRepository.all_chats()
        title = "Всі групи"
    else:
        chats = await ChatRepository.chat_by_type(chat_type)
        title = chat_type

    chats = chats or []
    await state.update_data(chats=chats, list_type=chat_type)

    if not chats:
        await edit_or_answer(
            callback,
            f"За типом <b>{title}</b> груп не знайдено.",
            reply_markup=kb_back_to_main()
        )
        await callback.answer()
        return

    await edit_or_answer(
        callback,
        f"Групи за типом <b>{title}</b> ({len(chats)}):",
        reply_markup=generate_pagination_groups(
            current_page=1, groups=chats, bulk_button=chat_type == "unspecified"
        )
    )
    await callback.answer()


@router.callback_query(GroupPageCallback.filter())
async def page_group_listener(callback: CallbackQuery, callback_data: GroupPageCallback, state: FSMContext):
    data = await state.get_data()
    chats = data.get("chats", [])
    await callback.message.edit_reply_markup(
        reply_markup=generate_pagination_groups(
            current_page=callback_data.page, groups=chats,
            bulk_button=data.get("list_type") == "unspecified"
        )
    )
    await callback.answer()


@router.callback_query(F.data == "page_noop")
async def noop_page_indicator(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(GroupCallback.filter())
async def about_group(callback: CallbackQuery, callback_data: GroupCallback):
    group = await ChatRepository.get_chat(callback_data.id)
    if not group:
        await callback.answer("Група не знайдена", show_alert=True)
        return

    user_count = await UserRepository.count_users_in_group(callback_data.id)

    type_cols = [t.value for t in TypeOfChats if t != TypeOfChats.ALL]
    categories = [CATEGORY_LABELS.get(col, col) for col in type_cols if getattr(group, col)]
    categories_text = "\n".join(categories) if categories else "Невизначена група"

    info = (
        f"<b>{hd.quote(group.title)}</b> (<code>{group.group_id}</code>)\n\n"
        f"<b>Категорії:</b>\n{categories_text}\n\n"
        f"<b>Учасників відстежується:</b> {user_count}\n"
        f"<b>Посилання:</b> {group.link or '—'}\n"
        f"Додана: {group.time}"
    )
    await edit_or_answer(callback, info, reply_markup=kb_group_info(str(group.group_id)))
    await callback.answer()


@router.callback_query(GroupSettingsCallback.filter())
async def show_group_settings(callback: CallbackQuery, callback_data: GroupSettingsCallback):
    group = await ChatRepository.get_chat(callback_data.group_id)
    if not group:
        await callback.answer("Група не знайдена", show_alert=True)
        return
    await edit_or_answer(
        callback,
        f"⚙️ Налаштування групи <b>{hd.quote(group.title)}</b>:",
        reply_markup=kb_group_settings(group)
    )
    await callback.answer()


@router.callback_query(GroupToggleCallback.filter())
async def toggle_group_category(callback: CallbackQuery, callback_data: GroupToggleCallback):
    group = await ChatRepository.get_chat(callback_data.group_id)
    if not group:
        await callback.answer("Група не знайдена", show_alert=True)
        return

    current_value = getattr(group, callback_data.category, False)
    await ChatRepository.update_chat_type(callback_data.group_id, callback_data.category, not current_value)

    updated = await ChatRepository.get_chat(callback_data.group_id)
    await callback.message.edit_reply_markup(reply_markup=kb_group_settings(updated))
    await callback.answer()


@router.callback_query(GroupDeleteCallback.filter(F.confirmed == False))
async def ask_delete_group(callback: CallbackQuery, callback_data: GroupDeleteCallback):
    group = await ChatRepository.get_chat(callback_data.group_id)
    name = group.title if group else callback_data.group_id
    await edit_or_answer(
        callback,
        f"Видалити групу <b>{hd.quote(name)}</b> з бази?",
        reply_markup=kb_group_delete_confirm(callback_data.group_id)
    )
    await callback.answer()


@router.callback_query(GroupDeleteCallback.filter(F.confirmed == True))
async def confirm_delete_group(callback: CallbackQuery, callback_data: GroupDeleteCallback):
    ok = await ChatRepository.remove_chat(callback_data.group_id)
    text = "✅ Групу видалено." if ok else "❌ Помилка при видаленні."
    await edit_or_answer(callback, text, reply_markup=kb_back_to_main())
    await callback.answer()
