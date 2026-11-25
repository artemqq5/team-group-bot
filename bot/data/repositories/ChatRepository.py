import logging
from typing import Optional

from sqlalchemy import and_, delete, func, select, update

from bot.data.db_connection import async_session, log_exc_with_try_except
from bot.data.models import ChatModel
from bot.data.other.accesses import TypeOfChats, has_value_enum


class ChatRepository:

    @staticmethod
    @log_exc_with_try_except()
    async def add_chat(group_id, title, datetime) -> Optional[ChatModel]:
        async with async_session() as session:
            async with session.begin():
                # PG: без NUL і не довше за varchar(255)
                title = title.replace("\x00", "")[:255] if title else title
                chat = ChatModel(group_id=str(group_id), title=title, time=datetime)
                session.add(chat)
                await session.flush()
                await session.refresh(chat)
                session.expunge(chat)
                return chat

    @staticmethod
    @log_exc_with_try_except()
    async def get_chat(group_id) -> Optional[ChatModel]:
        async with async_session() as session:
            result = await session.execute(
                select(ChatModel).where(ChatModel.group_id == str(group_id)).limit(1)
            )
            return result.scalar_one_or_none()

    @staticmethod
    async def remove_chat(group_id) -> bool:
        try:
            async with async_session() as session:
                async with session.begin():
                    result = await session.execute(
                        delete(ChatModel).where(ChatModel.group_id == str(group_id))
                    )
                    return result.rowcount > 0
        except Exception as e:
            logging.error(f"[ChatRepository] remove_chat error: {e}")
            return False

    @staticmethod
    async def update_chat_link(group_id, link) -> bool:
        try:
            async with async_session() as session:
                async with session.begin():
                    result = await session.execute(
                        update(ChatModel).values(link=link).where(ChatModel.group_id == str(group_id))
                    )
                    return result.rowcount > 0
        except Exception as e:
            logging.error(f"[ChatRepository] update_chat_link error: {e}")
            return False

    @staticmethod
    @log_exc_with_try_except()
    async def all_chats() -> list[ChatModel] | None:
        async with async_session() as session:
            result = await session.execute(select(ChatModel))
            return result.scalars().all()

    @staticmethod
    @log_exc_with_try_except()
    async def chat_by_type(chat_type) -> list[ChatModel] | None:
        if not has_value_enum(TypeOfChats, chat_type):
            return []
        async with async_session() as session:
            col = getattr(ChatModel, chat_type)
            result = await session.execute(select(ChatModel).where(col == True))
            return result.scalars().all()

    @staticmethod
    @log_exc_with_try_except()
    async def unspecified_chats() -> list[ChatModel] | None:
        async with async_session() as session:
            type_cols = [t for t in TypeOfChats if t != TypeOfChats.ALL]
            conditions = [getattr(ChatModel, t.value) == False for t in type_cols]
            result = await session.execute(select(ChatModel).where(and_(*conditions)))
            return result.scalars().all()

    @staticmethod
    async def update_chat_type(group_id, chat_type, available) -> bool:
        if not has_value_enum(TypeOfChats, chat_type):
            return False
        try:
            async with async_session() as session:
                async with session.begin():
                    result = await session.execute(
                        update(ChatModel)
                        .values({chat_type: bool(available)})
                        .where(ChatModel.group_id == str(group_id))
                    )
                    return result.rowcount > 0
        except Exception as e:
            logging.error(f"[ChatRepository] update_chat_type error: {e}")
            return False

    @staticmethod
    async def bulk_set_categories(group_ids, categories) -> int:
        categories = [
            c for c in categories
            if has_value_enum(TypeOfChats, c) and c != TypeOfChats.ALL.value
        ]
        if not group_ids or not categories:
            return 0
        try:
            async with async_session() as session:
                async with session.begin():
                    result = await session.execute(
                        update(ChatModel)
                        .values({c: True for c in categories})
                        .where(ChatModel.group_id.in_([str(g) for g in group_ids]))
                    )
                    return result.rowcount
        except Exception as e:
            logging.error(f"[ChatRepository] bulk_set_categories error: {e}")
            return 0

    @staticmethod
    @log_exc_with_try_except()
    async def count_chats_by_category() -> dict:
        async with async_session() as session:
            type_cols = [t for t in TypeOfChats if t != TypeOfChats.ALL]
            counts = {}
            for t in type_cols:
                col = getattr(ChatModel, t.value)
                result = await session.execute(select(func.count()).where(col == True))
                counts[t.value] = result.scalar_one() or 0
            conditions = [getattr(ChatModel, t.value) == False for t in type_cols]
            result = await session.execute(select(func.count()).where(and_(*conditions)))
            counts["unspecified"] = result.scalar_one() or 0
            return counts

    @staticmethod
    async def update_group_id(old_group_id, new_group_id) -> bool:
        try:
            async with async_session() as session:
                async with session.begin():
                    result = await session.execute(
                        update(ChatModel)
                        .values(group_id=str(new_group_id))
                        .where(ChatModel.group_id == str(old_group_id))
                    )
                    return result.rowcount > 0
        except Exception as e:
            logging.error(f"[ChatRepository] update_group_id error: {e}")
            return False
