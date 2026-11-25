import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from bot.data import db_connection
from bot.data.models import ChatMessageModel as M, ChatModel
from bot.data.other.accesses import TypeOfChats
from bot.domain.mcp.scope import scope_condition

MAX_GET_LIMIT = 100
MAX_SEARCH_LIMIT = 50
MAX_BYTES = 40_000
MAX_TEXT = 4000
MAX_RANGE = timedelta(days=31)
SEARCH_DEFAULT_WINDOW = timedelta(days=7)
SEARCH_MAX_WINDOW = timedelta(days=180)  # = типовий retention
CATEGORY_FLAGS = [t.value for t in TypeOfChats if t is not TypeOfChats.ALL]

NOT_FOUND = "chat not found"


class ToolError(Exception):
    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.message = message
        self.reason = reason  # bad_args | out_of_scope


TOOLS = [
    {
        "name": "list_chats",
        "description": "List Telegram group chats available to this key.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_messages",
        "description": "Read messages of one chat in ascending order (time is UTC). Range since..until is at most "
                       "31 days (until defaults to since+31d). Use next_cursor to continue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "since": {"type": "string", "description": "ISO-8601, naive = UTC"},
                "until": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_GET_LIMIT, "default": MAX_GET_LIMIT},
                "cursor": {"type": "string"},
            },
            "required": ["chat_id", "since"],
        },
    },
    {
        "name": "search_messages",
        "description": "Substring search in message text, newest first. Default window: last 7 days. If the result has \"truncated\": true, some matches were dropped: narrow the query or the date range.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 2},
                "chat_id": {"type": "string"},
                "since": {"type": "string"},
                "until": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_LIMIT, "default": MAX_SEARCH_LIMIT},
            },
            "required": ["query"],
        },
    },
]


def _parse_dt(value, field: str) -> datetime:
    if not isinstance(value, str):
        raise ToolError(f"{field}: ISO-8601 string expected", "bad_args")
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError):
        raise ToolError(f"{field}: invalid ISO-8601 datetime", "bad_args")
    return dt


def _limit(args: dict, maximum: int) -> int:
    value = args.get("limit", maximum)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ToolError("limit: positive integer expected", "bad_args")
    return min(value, maximum)


def _chat_id(args: dict, required: bool):
    value = args.get("chat_id")
    if value is None:
        if required:
            raise ToolError("chat_id is required", "bad_args")
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ToolError("chat_id: string expected", "bad_args")
    value = str(value)
    if "\x00" in value:  # PG не приймає NUL у bind-параметрах; такого чату існувати не може
        raise ToolError(NOT_FOUND, "out_of_scope")
    return value


def _iso(dt: datetime) -> str:
    return dt.isoformat() + "Z"


def _item(r) -> dict:
    text = r.text
    if text is not None and len(text) > MAX_TEXT:
        text = text[:MAX_TEXT]
    return {
        "message_id": r.message_id, "thread_id": r.thread_id, "reply_to_message_id": r.reply_to_message_id,
        "sent_at": _iso(r.sent_at), "author_id": r.author_id, "text": text,
        "message_type": r.message_type, "has_media": bool(r.has_media), "is_edited": bool(r.is_edited),
    }


def _pack(rows, with_chat: bool = False):
    """Збирає відповідь до ліміту ~40 KB. Повертає (authors, messages, truncated)."""
    authors, out, size = {}, [], 0
    truncated = False
    for r in rows:
        item = _item(r)
        if with_chat:
            item["chat_id"] = r.group_id
        new_author = r.author_id is not None and r.author_id not in authors
        extra = {"username": r.author_username, "first_name": r.author_first_name} if new_author else {}
        cost = len(json.dumps(item, ensure_ascii=False).encode()) + len(json.dumps(extra, ensure_ascii=False))
        if out and size + cost > MAX_BYTES:
            truncated = True
            break
        size += cost
        out.append((r, item))
        if new_author:
            authors[r.author_id] = extra
    return authors, out, truncated


async def _chat_in_scope(session, categories, chat_id: str) -> bool:
    row = await session.execute(
        select(ChatModel.group_id).where(ChatModel.group_id == chat_id, scope_condition(categories))
    )
    return row.first() is not None


def _joined_select(*cols):
    return select(*cols).select_from(M).join(ChatModel, ChatModel.group_id == M.group_id)


async def list_chats(categories, args: dict):
    async with db_connection.async_session() as session:
        rows = (await session.execute(
            select(ChatModel).where(scope_condition(categories)).order_by(ChatModel.title, ChatModel.group_id)
        )).scalars().all()
    allowed = {c for c in categories if isinstance(c, str)} if isinstance(categories, (list, tuple)) else set()
    all_scope = TypeOfChats.ALL.value in allowed
    chats = [{
        "chat_id": c.group_id, "title": c.title,
        # лише прапорці, що входять у scope ключа, — решту категорій чату не розкриваємо
        "categories": [f for f in CATEGORY_FLAGS if getattr(c, f) and (all_scope or f in allowed)],
    } for c in rows]
    return {"chats": chats}, len(chats)


async def get_messages(categories, args: dict):
    chat_id = _chat_id(args, True)
    since = _parse_dt(args.get("since"), "since")
    try:
        until = _parse_dt(args["until"], "until") if args.get("until") is not None else since + MAX_RANGE
    except OverflowError:
        raise ToolError("since: out of range", "bad_args")
    if until < since or until - since > MAX_RANGE:
        raise ToolError("until-since must be between 0 and 31 days", "bad_args")
    limit = _limit(args, MAX_GET_LIMIT)
    cursor = args.get("cursor")
    cur = None
    if cursor is not None:
        try:
            ts, _, cid = str(cursor).partition("|")
            cur_ts = datetime.fromisoformat(ts)
            if cur_ts.tzinfo is not None:  # колонки naive UTC — asyncpg не змішує aware/naive
                cur_ts = cur_ts.astimezone(timezone.utc).replace(tzinfo=None)
            cur = (cur_ts, int(cid))
            if not 0 < cur[1] < 2 ** 63:
                raise ValueError
        except (ValueError, OverflowError):
            raise ToolError("cursor: invalid", "bad_args")

    cond = [scope_condition(categories), M.group_id == chat_id, M.sent_at >= since, M.sent_at <= until]
    if cur:
        cond.append((M.sent_at > cur[0]) | ((M.sent_at == cur[0]) & (M.id > cur[1])))
    async with db_connection.async_session() as session:
        if not await _chat_in_scope(session, categories, chat_id):
            raise ToolError(NOT_FOUND, "out_of_scope")
        rows = (await session.execute(
            _joined_select(M).where(*cond).order_by(M.sent_at, M.id).limit(limit + 1)
        )).scalars().all()
    has_more = len(rows) > limit
    authors, packed, truncated = _pack(rows[:limit])
    next_cursor = None
    if has_more or truncated:
        last = packed[-1][0]
        next_cursor = f"{last.sent_at.isoformat()}|{last.id}"
    return {"authors": authors, "messages": [i for _, i in packed], "next_cursor": next_cursor}, len(packed)


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_messages(categories, args: dict):
    query = args.get("query")
    if not isinstance(query, str) or len(query) < 2:
        raise ToolError("query: at least 2 characters", "bad_args")
    if "\x00" in query:
        raise ToolError("query: invalid characters", "bad_args")
    chat_id = _chat_id(args, False)
    now = datetime.utcnow()
    until = _parse_dt(args["until"], "until") if args.get("until") is not None else now
    try:
        since = _parse_dt(args["since"], "since") if args.get("since") is not None else until - SEARCH_DEFAULT_WINDOW
    except OverflowError:
        raise ToolError("until: out of range", "bad_args")
    if until < since or until - since > SEARCH_MAX_WINDOW:
        raise ToolError("until-since must be between 0 and 180 days", "bad_args")
    limit = _limit(args, MAX_SEARCH_LIMIT)

    cond = [scope_condition(categories), M.sent_at >= since, M.sent_at <= until,
            M.text.ilike(f"%{_escape_like(query)}%", escape="\\")]
    if chat_id is not None:
        cond.append(M.group_id == chat_id)
    async with db_connection.async_session() as session:
        if chat_id is not None and not await _chat_in_scope(session, categories, chat_id):
            raise ToolError(NOT_FOUND, "out_of_scope")
        rows = (await session.execute(
            _joined_select(M).where(*cond).order_by(M.sent_at.desc(), M.id.desc()).limit(limit)
        )).scalars().all()
    authors, packed, truncated = _pack(rows, with_chat=True)
    return {"authors": authors, "messages": [i for _, i in packed], "truncated": truncated}, len(packed)


HANDLERS = {"list_chats": list_chats, "get_messages": get_messages, "search_messages": search_messages}
