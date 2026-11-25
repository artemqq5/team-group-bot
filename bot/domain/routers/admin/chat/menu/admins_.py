from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.data.repositories.AdminRepository import AdminRepository
from bot.domain.tools.edit_helper import edit_or_answer
from bot.presentation.keyboard.admin_ import (
    AdminMenuCallback, AdminSelectCallback, AdminActionCallback, AdminRoleCallback,
    MainMenuCallback, kb_admins_menu, kb_admins_list, kb_admin_actions,
    kb_admin_delete_confirm, kb_admin_roles, kb_cancel, kb_back_to_main
)
from bot.domain.states.ManageAdmin import ManageAdminState

router = Router()


@router.callback_query(MainMenuCallback.filter(F.action == "admins"))
async def show_admins_menu(callback: CallbackQuery):
    await edit_or_answer(callback, "👥 Управление админами:", reply_markup=kb_admins_menu())
    await callback.answer()


@router.callback_query(AdminMenuCallback.filter(F.action == "back"))
async def admins_back_to_menu(callback: CallbackQuery):
    await edit_or_answer(callback, "👥 Управление админами:", reply_markup=kb_admins_menu())
    await callback.answer()


@router.callback_query(AdminMenuCallback.filter(F.action == "list"))
async def show_admins_list(callback: CallbackQuery):
    admins = await AdminRepository.get_all_admins()
    if not admins:
        await callback.answer("Список админов пуст", show_alert=True)
        return
    await edit_or_answer(callback, "Список админов:", reply_markup=kb_admins_list(admins))
    await callback.answer()


@router.callback_query(AdminMenuCallback.filter(F.action == "add"))
async def start_add_admin(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ManageAdminState.telegram_id)
    await edit_or_answer(
        callback,
        "Введите <b>Telegram ID</b> нового админа:",
        reply_markup=kb_cancel()
    )
    await callback.answer()


@router.message(ManageAdminState.telegram_id)
async def get_admin_id(message: Message, state: FSMContext):
    tid = message.text.strip()
    if not tid.lstrip("-").isdigit():
        await message.answer("Введите числовой Telegram ID:", reply_markup=kb_cancel())
        return
    existing = await AdminRepository.is_admin(tid)
    if existing:
        await message.answer(f"Админ с ID <code>{tid}</code> уже существует.", reply_markup=kb_cancel())
        return
    await state.update_data(telegram_id=tid)
    await state.set_state(ManageAdminState.name)
    await message.answer("Введите имя админа:", reply_markup=kb_cancel())


@router.message(ManageAdminState.name)
async def get_admin_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    data = await state.get_data()
    await state.set_state(ManageAdminState.role)
    await message.answer(
        "Выберите роль админа:",
        reply_markup=kb_admin_roles(data["telegram_id"])
    )


@router.callback_query(AdminRoleCallback.filter(), ManageAdminState.role)
async def finish_add_admin(callback: CallbackQuery, callback_data: AdminRoleCallback, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    admin = await AdminRepository.add_admin(
        telegram_id=data["telegram_id"],
        name=data["name"],
        role=callback_data.role
    )
    if admin:
        text = (
            f"✅ Админ <b>{data['name']}</b> (<code>{data['telegram_id']}</code>) "
            f"с ролью <b>{callback_data.role}</b> добавлен."
        )
    else:
        text = "❌ Ошибка при добавлении админа."
    await edit_or_answer(callback, text, reply_markup=kb_admins_menu())
    await callback.answer()


@router.callback_query(AdminSelectCallback.filter())
async def select_admin(callback: CallbackQuery, callback_data: AdminSelectCallback):
    admin = await AdminRepository.is_admin(callback_data.telegram_id)
    if not admin:
        await callback.answer("Админ не найден", show_alert=True)
        return
    info = (
        f"👤 <b>{admin.name or 'Без имени'}</b>\n"
        f"ID: <code>{admin.telegram_id}</code>\n"
        f"Роль: <b>{admin.role}</b>"
    )
    await edit_or_answer(callback, info, reply_markup=kb_admin_actions(str(admin.telegram_id)))
    await callback.answer()


@router.callback_query(AdminActionCallback.filter(F.action == "change_role"))
async def change_admin_role(callback: CallbackQuery, callback_data: AdminActionCallback):
    await edit_or_answer(
        callback,
        "Выберите новую роль:",
        reply_markup=kb_admin_roles(callback_data.telegram_id)
    )
    await callback.answer()


@router.callback_query(AdminRoleCallback.filter())
async def set_admin_role(callback: CallbackQuery, callback_data: AdminRoleCallback, state: FSMContext):
    current_state = await state.get_state()
    if current_state == ManageAdminState.role:
        return
    ok = await AdminRepository.update_admin_role(callback_data.telegram_id, callback_data.role)
    if ok:
        admin = await AdminRepository.is_admin(callback_data.telegram_id)
        info = (
            f"✅ Роль изменена на <b>{callback_data.role}</b>\n\n"
            f"👤 <b>{admin.name or 'Без имени'}</b>\n"
            f"ID: <code>{admin.telegram_id}</code>\n"
            f"Роль: <b>{admin.role}</b>"
        )
        await edit_or_answer(callback, info, reply_markup=kb_admin_actions(callback_data.telegram_id))
    else:
        await edit_or_answer(callback, "❌ Ошибка при смене роли.", reply_markup=kb_admins_menu())
    await callback.answer()


@router.callback_query(AdminActionCallback.filter(F.action == "delete"))
async def ask_delete_admin(callback: CallbackQuery, callback_data: AdminActionCallback):
    admin = await AdminRepository.is_admin(callback_data.telegram_id)
    name = admin.name if admin else callback_data.telegram_id
    await edit_or_answer(
        callback,
        f"Удалить админа <b>{name}</b>?",
        reply_markup=kb_admin_delete_confirm(callback_data.telegram_id)
    )
    await callback.answer()


@router.callback_query(AdminActionCallback.filter(F.action == "confirm_delete"))
async def confirm_delete_admin(callback: CallbackQuery, callback_data: AdminActionCallback):
    ok = await AdminRepository.remove_admin(callback_data.telegram_id)
    text = "✅ Админ удалён." if ok else "❌ Ошибка при удалении."
    await edit_or_answer(callback, text, reply_markup=kb_admins_menu())
    await callback.answer()
