---
name: fastapi
description: How FastAPI is used in team-group-bot — a single app in bot/main.py that only serves the Telegram webhook (POST WEBHOOK_PATH with X-Telegram-Bot-Api-Secret-Token check), lifespan that sets the webhook and starts the bot-membership health-check task, uvicorn on port 8019 behind shared nginx. Use when working on bot/main.py, the webhook endpoint, lifespan/startup/shutdown, uvicorn startup, health check loop, 403 on webhook, or adding any HTTP endpoint.
---

# FastAPI in this project

FastAPI is a thin webhook host. Everything lives in `bot/main.py`:

- Module level: `load_dotenv()`, env (`BOT_TOKEN`, `WEBHOOK_BASE_URL`, `WEBHOOK_PATH`, `SECRET_TOKEN`, `HEALTH_CHECK_INTERVAL`, `LOG_PATH`), logging setup, `Dispatcher(storage=MemoryStorage())`, `Bot(..., parse_mode=HTML)`, `dp.include_routers(...)`.
- `lifespan`: `delete_webhook` → `set_webhook(WEBHOOK_BASE_URL + WEBHOOK_PATH, secret_token=SECRET_TOKEN, drop_pending_updates=True, allowed_updates=dp.resolve_used_update_types())` → starts `_health_check_loop` task; on shutdown cancels the task and closes `bot.session`.
- `@app.post(WEBHOOK_PATH)`: compares header `X-Telegram-Bot-Api-Secret-Token` with `SECRET_TOKEN` → `403` on mismatch; `dp.feed_update(bot, update)`; exceptions are logged and answered `{"ok": False}` (still 200, so Telegram does not retry).

Run: `uvicorn bot.main:app --host 0.0.0.0 --port 8019` (after `alembic upgrade head`, see `entrypoint.sh`). nginx on the server proxies to port 8019 via external `nginx-network`.

## Rules

- New router → import it in `bot/main.py` and add to `dp.include_routers(...)`; order matters (first matching handler wins; `events_` goes first).
- `drop_pending_updates=True` on start: updates sent during a restart are lost — keep in mind when changing deploy behaviour.
- Handlers must return fast; long work (mass broadcast) runs inside the update handling today — if it grows, move it to a background task, otherwise Telegram may time out and redeliver.
- No `@app.on_event` (deprecated) — use `lifespan`.
- Adding other HTTP endpoints is new scope — only on the user's explicit request.
- Importing `bot.main` creates a `Bot` with the env token and opens a log file — tests must not import it without env; test routers/repos directly.
