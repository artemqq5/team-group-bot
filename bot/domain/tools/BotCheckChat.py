import asyncio
import logging
from html import escape

from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter

from bot.data.repositories.ChatRepository import ChatRepository


def _chat_label(chat) -> str:
    title = escape(str(chat.title or chat.group_id))
    return f"<b>{title}</b> (<code>{chat.group_id}</code>)"


async def _send_safe(bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except TelegramRetryAfter as e:
        logging.warning(f"health_check: flood control, waiting {e.retry_after}s")
        await asyncio.sleep(e.retry_after)
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logging.error(f"health_check: failed to notify admin: {e}")


async def check_bot_membership(bot, chat_id: int) -> None:
    chats = await ChatRepository.all_chats()
    if not chats:
        return

    bot_id = (await bot.get_me()).id
    removed: list[str] = []
    errors: list[str] = []

    for chat in chats:
        label = _chat_label(chat)
        try:
            await bot.get_chat_member(chat_id=chat.group_id, user_id=bot_id)
        except TelegramForbiddenError:
            ok = await ChatRepository.remove_chat(chat.group_id)
            removed.append(f"⛔ {label} — {'удалено ✅' if ok else 'не удалось ❌'}")
            logging.info(f"health_check: kicked from {chat.group_id}, removed={ok}")
        except TelegramBadRequest as e:
            if "chat not found" in e.message.lower():
                ok = await ChatRepository.remove_chat(chat.group_id)
                removed.append(f"🗑 {label} — {'удалено ✅' if ok else 'не удалось ❌'}")
                logging.info(f"health_check: not found {chat.group_id}, removed={ok}")
            else:
                errors.append(f"⚠️ {label}: {escape(e.message)}")
                logging.warning(f"health_check: bad request {chat.group_id}: {e.message}")
        except Exception as e:
            logging.error(f"health_check: unexpected {chat.group_id}: {e}")

        await asyncio.sleep(0.05)

    total = len(chats)
    issues = removed + errors
    logging.info(f"health_check: {total} chats checked, {len(issues)} issues")

    if issues:
        lines = [f"<b>🔍 Health check: {len(issues)} проблем из {total} групп</b>", ""] + issues
        chunk, size = [], 0
        for line in lines:
            if size + len(line) + 1 > 4000 and chunk:
                await _send_safe(bot, chat_id, "\n".join(chunk))
                chunk, size = [], 0
            chunk.append(line)
            size += len(line) + 1
        if chunk:
            await _send_safe(bot, chat_id, "\n".join(chunk))
