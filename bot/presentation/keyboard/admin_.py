import math

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.data.models import ChatModel
from bot.data.other.accesses import access_admin_to_chat, TypeOfAdmins, TypeOfChats


# ─── Callback Data ────────────────────────────────────────────────────────────

class MainMenuCallback(CallbackData, prefix="mm"):
    action: str


class ShowTypeCallback(CallbackData, prefix="st"):
    chat_type: str


class GroupCallback(CallbackData, prefix="group*callback"):
    id: str


class GroupPageCallback(CallbackData, prefix="group*page*callback"):
    page: int


class GroupSettingsCallback(CallbackData, prefix="gs"):
    group_id: str


class GroupToggleCallback(CallbackData, prefix="gt"):
    group_id: str
    category: str


class GroupDeleteCallback(CallbackData, prefix="gd"):
    group_id: str
    confirmed: bool = False


class BulkToggleCallback(CallbackData, prefix="bkt"):
    group_id: str
    page: int


class BulkPageCallback(CallbackData, prefix="bkp"):
    page: int


class BulkActionCallback(CallbackData, prefix="bka"):
    action: str
    page: int = 1


class BulkCategoryCallback(CallbackData, prefix="bkc"):
    category: str


class AdminMenuCallback(CallbackData, prefix="am"):
    action: str


class AdminSelectCallback(CallbackData, prefix="asel"):
    telegram_id: str


class AdminActionCallback(CallbackData, prefix="aact"):
    telegram_id: str
    action: str


class AdminRoleCallback(CallbackData, prefix="arol"):
    telegram_id: str
    role: str


class BroadcastCategoryCallback(CallbackData, prefix="bcc"):
    category: str


class BroadcastActionCallback(CallbackData, prefix="ba"):
    action: str


class BroadcastHistoryCallback(CallbackData, prefix="bh"):
    action: str
    record_id: int = 0


# ─── Main Menu ────────────────────────────────────────────────────────────────

def kb_main(admin) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Переглянути групи", callback_data=MainMenuCallback(action="groups"))
    kb.button(text="📤 Розсилка", callback_data=MainMenuCallback(action="broadcast"))
    if admin.role == TypeOfAdmins.ADMIN.value:
        kb.button(text="📊 Статистика", callback_data=MainMenuCallback(action="stats"))
        kb.button(text="👥 Адміни", callback_data=MainMenuCallback(action="admins"))
    kb.adjust(1)
    return kb.as_markup()


def kb_back_to_main() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Головне меню", callback_data=MainMenuCallback(action="back"))
    return kb.as_markup()


def kb_cancel() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Скасувати", callback_data=MainMenuCallback(action="cancel"))
    return kb.as_markup()


def kb_skip_cancel() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустити", callback_data=BroadcastActionCallback(action="skip"))
    kb.button(text="✖️ Скасувати", callback_data=MainMenuCallback(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


def kb_yes_skip_cancel() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Так", callback_data=BroadcastActionCallback(action="yes"))
    kb.button(text="⏭ Пропустити", callback_data=BroadcastActionCallback(action="skip"))
    kb.button(text="✖️ Скасувати", callback_data=MainMenuCallback(action="cancel"))
    kb.adjust(2, 1)
    return kb.as_markup()


def kb_send_cancel() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Відправити", callback_data=BroadcastActionCallback(action="send"))
    kb.button(text="✖️ Скасувати", callback_data=MainMenuCallback(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


# ─── Group Type Selection ─────────────────────────────────────────────────────

def kb_type_group(admin) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if admin.role == TypeOfAdmins.ADMIN.value:
        kb.button(text="🔘 Невизначені групи", callback_data=ShowTypeCallback(chat_type="unspecified"))
    for chat_type in access_admin_to_chat[admin.role]:
        kb.button(text=chat_type, callback_data=ShowTypeCallback(chat_type=chat_type))
    kb.adjust(2, repeat=True)
    kb.row(InlineKeyboardButton(text="↩️ Головне меню", callback_data=MainMenuCallback(action="back").pack()))
    return kb.as_markup()


def kb_messaging_category(admin) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for chat_type in access_admin_to_chat[admin.role]:
        kb.button(text=chat_type, callback_data=BroadcastCategoryCallback(category=chat_type))
    kb.button(text="↩️ Назад", callback_data=MainMenuCallback(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


# ─── Group List & Pagination ──────────────────────────────────────────────────

def _group_labels(groups) -> dict[str, str]:
    from collections import Counter
    title_counts = Counter(g.title for g in groups)
    return {
        str(g.group_id): f"{g.title} (…{str(g.group_id)[-6:]})" if title_counts[g.title] > 1 else g.title
        for g in groups
    }


def generate_pagination_groups(current_page: int, groups, bulk_button: bool = False) -> InlineKeyboardMarkup:
    total_pages = math.ceil(len(groups) / 10) if groups else 1
    labels = _group_labels(groups)
    kb = InlineKeyboardBuilder()

    start_index = (current_page - 1) * 10
    end_index = min(start_index + 10, len(groups))

    for i in range(start_index, end_index):
        g = groups[i]
        kb.button(text=labels[str(g.group_id)], callback_data=GroupCallback(id=str(g.group_id)))
    kb.adjust(1)

    kb.row(
        InlineKeyboardButton(
            text="⬅️" if current_page > 1 else " ",
            callback_data=GroupPageCallback(page=current_page - 1).pack() if current_page > 1 else "page_noop"
        ),
        InlineKeyboardButton(
            text=f"{current_page}/{total_pages}",
            callback_data="page_noop"
        ),
        InlineKeyboardButton(
            text="➡️" if current_page < total_pages else " ",
            callback_data=GroupPageCallback(page=current_page + 1).pack() if current_page < total_pages else "page_noop"
        ),
    )
    if bulk_button:
        kb.row(InlineKeyboardButton(
            text="☑️ Вибрати кілька",
            callback_data=BulkActionCallback(action="start").pack()
        ))
    kb.row(InlineKeyboardButton(
        text="↩️ Назад",
        callback_data=MainMenuCallback(action="back").pack()
    ))
    return kb.as_markup()


# ─── Bulk Move ────────────────────────────────────────────────────────────────

def kb_bulk_select(current_page: int, groups, selected: set[str]) -> InlineKeyboardMarkup:
    total_pages = math.ceil(len(groups) / 10) if groups else 1
    labels = _group_labels(groups)
    kb = InlineKeyboardBuilder()

    start_index = (current_page - 1) * 10
    end_index = min(start_index + 10, len(groups))

    for i in range(start_index, end_index):
        gid = str(groups[i].group_id)
        prefix = "✅" if gid in selected else "⬜"
        kb.button(
            text=f"{prefix} {labels[gid]}",
            callback_data=BulkToggleCallback(group_id=gid, page=current_page)
        )
    kb.adjust(1)

    kb.row(
        InlineKeyboardButton(
            text="☑️ Вся сторінка",
            callback_data=BulkActionCallback(action="select_page", page=current_page).pack()
        ),
        InlineKeyboardButton(
            text="🧹 Зняти все",
            callback_data=BulkActionCallback(action="clear", page=current_page).pack()
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="⬅️" if current_page > 1 else " ",
            callback_data=BulkPageCallback(page=current_page - 1).pack() if current_page > 1 else "page_noop"
        ),
        InlineKeyboardButton(
            text=f"{current_page}/{total_pages}",
            callback_data="page_noop"
        ),
        InlineKeyboardButton(
            text="➡️" if current_page < total_pages else " ",
            callback_data=BulkPageCallback(page=current_page + 1).pack() if current_page < total_pages else "page_noop"
        ),
    )
    kb.row(InlineKeyboardButton(
        text=f"➡️ Перенести ({len(selected)})",
        callback_data=BulkActionCallback(action="move", page=current_page).pack()
    ))
    kb.row(InlineKeyboardButton(
        text="✖️ Скасувати",
        callback_data=BulkActionCallback(action="cancel").pack()
    ))
    return kb.as_markup()


def kb_bulk_categories(selected_cats: set[str], page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for cat, label in CATEGORY_LABELS.items():
        prefix = "✅" if cat in selected_cats else "❌"
        kb.button(text=f"{prefix} {label}", callback_data=BulkCategoryCallback(category=cat))
    kb.adjust(2)
    kb.row(InlineKeyboardButton(
        text="✅ Підтвердити",
        callback_data=BulkActionCallback(action="confirm", page=page).pack()
    ))
    kb.row(InlineKeyboardButton(
        text="↩️ Назад до вибору",
        callback_data=BulkActionCallback(action="back_select", page=page).pack()
    ))
    return kb.as_markup()


def kb_bulk_done() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔘 До невизначених", callback_data=ShowTypeCallback(chat_type="unspecified"))
    kb.button(text="↩️ Головне меню", callback_data=MainMenuCallback(action="back"))
    kb.adjust(1)
    return kb.as_markup()


# ─── Group Info & Settings ────────────────────────────────────────────────────

CATEGORY_LABELS = {
    "agency_fb": "Agency FB",
    "agency_google": "Agency Google",
    "apps": "Apps",
    "shop_google": "Shop Google",
    "shop_fb": "Shop FB",
    "console": "Console",
    "creo": "Creo",
    "affiliate_mp": "Affiliate MP",
    "partner_mp": "Partner MP",
    "media_mt": "Media MT",
    "media_mp": "Media MP",
    "partner_mt": "Partner MT",
}


def kb_group_info(group_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⚙️ Налаштування", callback_data=GroupSettingsCallback(group_id=group_id))
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="↩️ До списку", callback_data=MainMenuCallback(action="groups").pack()))
    return kb.as_markup()


def kb_group_settings(chat: ChatModel) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for cat, label in CATEGORY_LABELS.items():
        is_active = getattr(chat, cat, False)
        prefix = "✅" if is_active else "❌"
        kb.button(
            text=f"{prefix} {label}",
            callback_data=GroupToggleCallback(group_id=str(chat.group_id), category=cat)
        )
    kb.adjust(2)
    kb.row(InlineKeyboardButton(
        text="🗑 Видалити групу",
        callback_data=GroupDeleteCallback(group_id=str(chat.group_id), confirmed=False).pack()
    ))
    kb.row(InlineKeyboardButton(
        text="↩️ До групи",
        callback_data=GroupCallback(id=str(chat.group_id)).pack()
    ))
    return kb.as_markup()


def kb_group_delete_confirm(group_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Так, видалити", callback_data=GroupDeleteCallback(group_id=group_id, confirmed=True))
    kb.button(text="↩️ Назад", callback_data=GroupSettingsCallback(group_id=group_id))
    kb.adjust(2)
    return kb.as_markup()


# ─── Admin Management ─────────────────────────────────────────────────────────

def kb_admins_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Список адмінів", callback_data=AdminMenuCallback(action="list"))
    kb.button(text="➕ Додати адміна", callback_data=AdminMenuCallback(action="add"))
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="⬅️ Головне меню", callback_data=MainMenuCallback(action="back").pack()))
    return kb.as_markup()


def kb_admins_list(admins: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for admin in admins:
        label = f"{admin.name or admin.telegram_id} ({admin.role})"
        kb.button(text=label, callback_data=AdminSelectCallback(telegram_id=str(admin.telegram_id)))
    kb.adjust(2, repeat=True)
    kb.row(InlineKeyboardButton(text="↩️ Назад", callback_data=AdminMenuCallback(action="back").pack()))
    return kb.as_markup()


def kb_admin_actions(telegram_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Змінити роль", callback_data=AdminActionCallback(telegram_id=telegram_id, action="change_role"))
    kb.button(text="🗑 Видалити", callback_data=AdminActionCallback(telegram_id=telegram_id, action="delete"))
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="↩️ Назад", callback_data=AdminMenuCallback(action="list").pack()))
    return kb.as_markup()


def kb_admin_delete_confirm(telegram_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Так, видалити", callback_data=AdminActionCallback(telegram_id=telegram_id, action="confirm_delete"))
    kb.button(text="↩️ Назад", callback_data=AdminSelectCallback(telegram_id=telegram_id))
    kb.adjust(2)
    return kb.as_markup()


def kb_admin_roles(telegram_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for role in TypeOfAdmins:
        kb.button(text=role.value, callback_data=AdminRoleCallback(telegram_id=telegram_id, role=role.value))
    kb.adjust(2, repeat=True)
    kb.row(InlineKeyboardButton(text="↩️ Назад", callback_data=AdminSelectCallback(telegram_id=telegram_id).pack()))
    return kb.as_markup()


# ─── Broadcast Menu & History ─────────────────────────────────────────────────

def kb_broadcast_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Нова розсилка", callback_data=BroadcastActionCallback(action="new"))
    kb.button(text="📜 Історія розсилок", callback_data=BroadcastHistoryCallback(action="list", record_id=0))
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="↩️ Назад", callback_data=MainMenuCallback(action="back").pack()))
    return kb.as_markup()


def kb_broadcast_history(records: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for r in records:
        date_str = r.sent_at.strftime("%d.%m %H:%M")
        label = f"{date_str} | {r.category} | {r.groups_count} груп"
        kb.button(text=label, callback_data=BroadcastHistoryCallback(action="view", record_id=r.id))
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="↩️ Назад", callback_data=MainMenuCallback(action="broadcast").pack()))
    return kb.as_markup()


def kb_broadcast_history_back() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ До списку", callback_data=BroadcastHistoryCallback(action="list", record_id=0))
    kb.adjust(1)
    return kb.as_markup()
