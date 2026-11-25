from sqlalchemy import ColumnElement, false, or_, true

from bot.data.models import ChatModel
from bot.data.other.accesses import TypeOfChats, has_value_enum


def scope_condition(categories) -> ColumnElement:
    """Чиста функція: категорії ключа -> умова на ChatModel. Невідомі значення відкидаються."""
    if not isinstance(categories, (list, tuple)):
        return false()
    cats = [c for c in categories if isinstance(c, str) and has_value_enum(TypeOfChats, c)]
    if not cats:
        return false()
    if TypeOfChats.ALL.value in cats:
        return true()  # усі рядки в chats (разом з unspecified)
    return or_(*(getattr(ChatModel, c) == True for c in cats))  # noqa: E712
