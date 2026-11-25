import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Message

from bot.data import db_connection
from bot.data.repositories import MessageRepository

GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
MEDIA_TYPES = {
    "photo", "video", "audio", "voice", "document", "animation", "sticker",
    "video_note", "contact", "location", "venue", "poll", "dice",
}


def _naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _clean(value, limit=None):
    """NUL і надто довгі рядки PG не приймає; ідентифікатори сюди не передаємо."""
    if value is None:
        return None
    value = value.replace("\x00", "")
    return value[:limit] if limit else value


def _values(message: Message) -> dict:
    author_id = author_username = author_first_name = None
    if message.from_user:
        author_id = str(message.from_user.id)
        author_username = message.from_user.username
        author_first_name = message.from_user.first_name
    if message.sender_chat:  # анонімні адміни / канали
        author_id = str(message.sender_chat.id)
        author_username = message.sender_chat.username
        author_first_name = message.sender_chat.title
    content_type = str(message.content_type.value if hasattr(message.content_type, "value") else message.content_type)
    return {
        "group_id": str(message.chat.id),
        "message_id": message.message_id,
        "thread_id": message.message_thread_id,
        "reply_to_message_id": message.reply_to_message.message_id if message.reply_to_message else None,
        "author_id": author_id,
        "author_username": _clean(author_username, 255),
        "author_first_name": _clean(author_first_name, 255),
        "text": _clean(message.text or message.caption),
        "message_type": content_type[:32],
        "has_media": content_type in MEDIA_TYPES,
        "sent_at": _naive_utc(message.date),
        # у aiogram 3.4.1 edit_date — int (unix seconds)
        "edit_date": datetime.utcfromtimestamp(message.edit_date) if message.edit_date else None,
        "is_edited": bool(message.edit_date),
    }


class MessageCapture(BaseMiddleware):
    def __init__(self, edited: bool):
        self.edited = edited

    async def __call__(
            self,
            handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
            event: Message,
            data: Dict[str, Any],
    ) -> Any:
        try:
            if event.chat.type in GROUP_TYPES:
                values = _values(event)

                async def _write():
                    async with db_connection.engine.begin() as conn:
                        await MessageRepository.upsert_message(conn, values, self.edited)

                await asyncio.wait_for(_write(), 3)
        except Exception as e:
            # текст повідомлення ніколи не логуємо
            logging.error(f"message_capture failed: {type(e).__name__}")
        return await handler(event, data)
