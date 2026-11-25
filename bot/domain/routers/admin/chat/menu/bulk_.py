from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from bot.data.other.accesses import TypeOfAdmins
from bot.data.repositories.AdminRepository import AdminRepository
from bot.data.repositories.ChatRepository import ChatRepository
from bot.domain.tools.edit_helper import edit_or_answer
from bot.presentation.keyboard.admin_ import (
    BulkToggleCallback, BulkPageCallback, BulkActionCallback, BulkCategoryCallback,
    kb_bulk_select, kb_bulk_categories, kb_bulk_done, kb_back_to_main,
    generate_pagination_groups, CATEGORY_LABELS
)

router = Router()


async def _is_main_admin(callback: CallbackQuery) -> bool:
    admin = await AdminRepository.is_admin(callback.from_user.id)
    if not admin or admin.role != TypeOfAdmins.ADMIN.value:
        await callback.answer("Нет доступа", show_alert=True)
        return False
    return True


def _select_text(selected: set[str], total: int) -> str:
    return (
        f"☑️ <b>Выбор неопределённых групп</b>\n\n"
        f"Выбрано: <b>{len(selected)}</b> из {total}\n"
        f"Отметьте группы и нажмите «Перенести»."
    )


async def _render_select(callback: CallbackQuery, state: FSMContext, page: int):
    data = await state.get_data()
    chats = data.get("chats", [])
    selected = set(data.get("bulk_selected", []))
    await edit_or_answer(
        callback,
        _select_text(selected, len(chats)),
        reply_markup=kb_bulk_select(page, chats, selected)
    )


@router.callback_query(BulkActionCallback.filter(F.action == "start"))
async def bulk_start(callback: CallbackQuery, state: FSMContext):
    if not await _is_main_admin(callback):
        return
    chats = await ChatRepository.unspecified_chats() or []
    if not chats:
        await edit_or_answer(callback, "Неопределённых групп нет.", reply_markup=kb_back_to_main())
        await callback.answer()
        return
    await state.update_data(chats=chats, list_type="unspecified", bulk_selected=[], bulk_cats=[])
    await _render_select(callback, state, page=1)
    await callback.answer()


@router.callback_query(BulkToggleCallback.filter())
async def bulk_toggle(callback: CallbackQuery, callback_data: BulkToggleCallback, state: FSMContext):
    data = await state.get_data()
    selected = set(data.get("bulk_selected", []))
    selected ^= {callback_data.group_id}
    await state.update_data(bulk_selected=list(selected))
    await _render_select(callback, state, page=callback_data.page)
    await callback.answer()


@router.callback_query(BulkPageCallback.filter())
async def bulk_page(callback: CallbackQuery, callback_data: BulkPageCallback, state: FSMContext):
    await _render_select(callback, state, page=callback_data.page)
    await callback.answer()


@router.callback_query(BulkActionCallback.filter(F.action == "select_page"))
async def bulk_select_page(callback: CallbackQuery, callback_data: BulkActionCallback, state: FSMContext):
    data = await state.get_data()
    chats = data.get("chats", [])
    selected = set(data.get("bulk_selected", []))
    start = (callback_data.page - 1) * 10
    selected |= {str(c.group_id) for c in chats[start:start + 10]}
    await state.update_data(bulk_selected=list(selected))
    await _render_select(callback, state, page=callback_data.page)
    await callback.answer()


@router.callback_query(BulkActionCallback.filter(F.action == "clear"))
async def bulk_clear(callback: CallbackQuery, callback_data: BulkActionCallback, state: FSMContext):
    await state.update_data(bulk_selected=[])
    await _render_select(callback, state, page=callback_data.page)
    await callback.answer()


@router.callback_query(BulkActionCallback.filter(F.action.in_(["move", "back_select"])))
async def bulk_move_or_back(callback: CallbackQuery, callback_data: BulkActionCallback, state: FSMContext):
    if callback_data.action == "back_select":
        await _render_select(callback, state, page=callback_data.page)
        await callback.answer()
        return

    data = await state.get_data()
    selected = data.get("bulk_selected", [])
    if not selected:
        await callback.answer("Ничего не выбрано", show_alert=True)
        return
    cats = set(data.get("bulk_cats", []))
    await state.update_data(bulk_page=callback_data.page)
    await edit_or_answer(
        callback,
        f"Выберите категории для <b>{len(selected)}</b> групп:",
        reply_markup=kb_bulk_categories(cats, callback_data.page)
    )
    await callback.answer()


@router.callback_query(BulkCategoryCallback.filter())
async def bulk_toggle_category(callback: CallbackQuery, callback_data: BulkCategoryCallback, state: FSMContext):
    if callback_data.category not in CATEGORY_LABELS:
        await callback.answer()
        return
    data = await state.get_data()
    cats = set(data.get("bulk_cats", []))
    cats ^= {callback_data.category}
    await state.update_data(bulk_cats=list(cats))
    await callback.message.edit_reply_markup(
        reply_markup=kb_bulk_categories(cats, data.get("bulk_page", 1))
    )
    await callback.answer()


@router.callback_query(BulkActionCallback.filter(F.action == "confirm"))
async def bulk_confirm(callback: CallbackQuery, state: FSMContext):
    if not await _is_main_admin(callback):
        return
    data = await state.get_data()
    selected = data.get("bulk_selected", [])
    cats = data.get("bulk_cats", [])
    if not selected:
        await callback.answer("Ничего не выбрано", show_alert=True)
        return
    if not cats:
        await callback.answer("Выберите хотя бы одну категорию", show_alert=True)
        return

    moved = await ChatRepository.bulk_set_categories(selected, cats)
    await state.update_data(bulk_selected=[], bulk_cats=[])

    labels = ", ".join(CATEGORY_LABELS[c] for c in CATEGORY_LABELS if c in cats)
    text = (
        f"✅ Перенесено <b>{moved}</b> групп в: {labels}"
        if moved else "❌ Не удалось перенести группы."
    )
    await edit_or_answer(callback, text, reply_markup=kb_bulk_done())
    await callback.answer()


@router.callback_query(BulkActionCallback.filter(F.action == "cancel"))
async def bulk_cancel(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    chats = data.get("chats", [])
    await state.update_data(bulk_selected=[], bulk_cats=[])
    await edit_or_answer(
        callback,
        f"Группы по типу <b>Неопределённые группы</b> ({len(chats)}):",
        reply_markup=generate_pagination_groups(current_page=1, groups=chats, bulk_button=True)
    )
    await callback.answer()
