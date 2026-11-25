---
name: aiogram
description: Conventions for building the project Telegram bot on aiogram 3.x — Bot/Dispatcher setup, Router per feature, magic filter F, handlers, FSM (StatesGroup, FSMContext, RedisStorage/MemoryStorage), inline/reply keyboards, CallbackData factories, middlewares (DB session injection, throttling), error handlers, polling vs webhook, graceful shutdown, testing via feed_update. Use when working on bot handlers, routers, keyboards, callback_query, message handlers, FSM states, middlewares, bot startup (__main__), webhook, or aiogram errors (TelegramBadRequest, TelegramRetryAfter).
---

# aiogram 3.x

## This project (team-group-bot)

- aiogram **3.4.1**, webhook mode (FastAPI in `bot/main.py`, see `fastapi` skill) — no polling entry point. `MemoryStorage` FSM: state is lost on restart.
- Routers: `bot/domain/routers/events_.py` (bot added/removed → `ChatRepository`), `admin/chat/main_chat.py` + `admin/chat/menu/*_.py` (private-chat admin menu), `admin/group/main_group.py` + `menu/update_.py` (in-group admin commands), `user/main_.py`. Register new ones in `dp.include_routers(...)` in `bot/main.py`.
- Access control: filters `bot/domain/filters/` (`IsGroup`, `IsMainAdmin`), middlewares `bot/domain/middlewares/` (`IsPrivateChatAdmin`, `IsGroupAdmin`, `IsGroupUser`). FSM states in `bot/domain/states/`, keyboards in `bot/presentation/keyboard/admin_.py`, helpers (`MessageSpamTool`, `BotCheckChat`, `edit_helper`) in `bot/domain/tools/`.
- No DB-session middleware: handlers call static repositories (see `sqlalchemy-async`). Sections below are general aiogram reference — follow the project layout above where they differ.

Target: **aiogram 3.x** (not 2.x — no `executor`, no `dp.message_handler`, no `Bot.set_current`). If unsure about an API — check docs via context7 (`aiogram`) before writing.

## Entry point

```python
import asyncio, logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()                       # storage=MemoryStorage() за замовчуванням
    dp.include_routers(start.router, orders.router)
    dp.update.middleware(DbSessionMiddleware(session_factory))
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)       # закрити engine: await engine.dispose()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
```

- `parse_mode` — via `DefaultBotProperties`, not `Bot(parse_mode=...)` (removed in 3.7+).
- Extra deps for handlers: `dp["settings"] = settings` or `dp.start_polling(bot, settings=settings)` → handler receives `settings` kwarg by name.

## Routers & handlers

One `Router` per feature module (`handlers/<feature>.py`), exported as `router`. Handlers are thin: parse input → call service → answer. Business logic lives in services, not in handlers.

```python
from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery

router = Router(name="orders")

@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession) -> None:
    user = await users.get_or_create(session, message.from_user.id)
    await message.answer(f"Привіт, {html.quote(message.from_user.full_name)}")

@router.callback_query(OrderCb.filter(F.action == "take"))
async def take_order(cb: CallbackQuery, callback_data: OrderCb, session: AsyncSession) -> None:
    ...
    await cb.answer()   # ЗАВЖДИ відповідати на callback, інакше «годинник» на кнопці
```

- Filters: `F.text`, `F.photo`, `F.chat.type == "private"`, `Command("help")`. Router-level: `router.message.filter(F.chat.type == "private")`.
- Order matters: first matching handler wins; catch-all handlers go into the last router.
- Escape user text in HTML: `from aiogram.utils.markdown import html_decoration as hd` / `html.quote`.

## Callback data

Use `CallbackData` factories, never hand-parsed strings. Payload ≤ 64 bytes — pass ids, not objects.

```python
from aiogram.filters.callback_data import CallbackData

class OrderCb(CallbackData, prefix="ord"):
    action: str
    order_id: int
```

Keyboards via builders:

```python
from aiogram.utils.keyboard import InlineKeyboardBuilder
kb = InlineKeyboardBuilder()
kb.button(text="Взяти", callback_data=OrderCb(action="take", order_id=o.id))
kb.adjust(2)
await message.answer("...", reply_markup=kb.as_markup())
```

## FSM

```python
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

class NewOrder(StatesGroup):
    title = State()
    budget = State()

@router.message(NewOrder.title, F.text)
async def got_title(message: Message, state: FSMContext) -> None:
    await state.update_data(title=message.text)
    await state.set_state(NewOrder.budget)
```

- Always `await state.clear()` on finish/cancel; provide `/cancel` handler with `StateFilter("*")`.
- State data must be JSON-serializable (ids, str, int) — no ORM objects.
- `MemoryStorage` loses state on restart; for prod use `RedisStorage` (`aiogram.fsm.storage.redis`).

## Middlewares

DB session per update (outer middleware on `dp.update`):

```python
from aiogram import BaseMiddleware

class DbSessionMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(self, handler, event, data):
        async with self.session_factory() as session:
            data["session"] = session
            return await handler(event, data)
```

- Commit explicitly in services/handlers (or wrap `session.begin()` in middleware — choose one, document it in CLAUDE.md).
- `dp.update.outer_middleware` runs before filters; `router.message.middleware` runs after filters matched.

## Errors & limits

- Global: `@router.errors()` / `dp.errors.register(handler)` → receives `ErrorEvent` (`event.exception`, `event.update`). Log with `logging.exception`, don't swallow silently.
- `TelegramRetryAfter` → sleep `e.retry_after`, retry. `TelegramForbiddenError` → user blocked bot, mark inactive. `TelegramBadRequest("message is not modified")` → ignore on edits.
- Broadcasts: ≤ ~30 msg/s overall, 1 msg/s per chat; send with `asyncio.sleep` throttle, never `gather` thousands at once.
- Never do blocking I/O (requests, time.sleep, sync DB) inside handlers.

## Webhook (if chosen instead of polling)

`aiogram.webhook.aiohttp_server.SimpleRequestHandler` + `setup_application`, `bot.set_webhook(url, secret_token=...)`; verify `X-Telegram-Bot-Api-Secret-Token`. Pick one mode — do not run polling and webhook together.

## Testing

No real network. Build `Update` via `Update.model_validate({...})` and call `await dp.feed_update(bot, update)` with a `Bot` whose session is mocked (`bot.session` replaced / `AsyncMock` on `bot.__call__`). Assert on called methods (`SendMessage`, `AnswerCallbackQuery`).
