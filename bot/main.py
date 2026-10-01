import asyncio
import logging
import os
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException

import bot.domain.routers.admin.chat.main_chat as main_chat_module
import bot.domain.routers.admin.group.main_group as main_group_module
from bot.data.repositories.AdminRepository import AdminRepository
from bot.domain.routers import events_
from bot.domain.routers.user import main_
from bot.domain.tools.BotCheckChat import check_bot_membership

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH")
SECRET_TOKEN = os.getenv("SECRET_TOKEN")
HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", 6 * 3600))

log_path = os.getenv("LOG_PATH", "logs/app.log")
os.makedirs(os.path.dirname(log_path), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()],
    force=True,
)

storage = MemoryStorage()
dp = Dispatcher(storage=storage)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

dp.include_routers(
    events_.router,
    main_chat_module.router,
    main_group_module.router,
    main_.router,
)


async def _health_check_loop(bot: Bot):
    await asyncio.sleep(60)  # перша перевірка через хвилину після старту
    while True:
        logging.info("health_check: starting background group check")
        try:
            admins = await AdminRepository.get_admins()
            if admins:
                await check_bot_membership(bot, admins[0].telegram_id)
        except Exception as e:
            logging.error(f"health_check: {e}")
        logging.info(f"health_check: done, next in {HEALTH_CHECK_INTERVAL // 3600}h")
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await bot.delete_webhook()
    await bot.set_webhook(url=WEBHOOK_BASE_URL + WEBHOOK_PATH, secret_token=SECRET_TOKEN, drop_pending_updates=True,
                          allowed_updates=dp.resolve_used_update_types())
    logging.info(f"Webhook set: {WEBHOOK_BASE_URL + WEBHOOK_PATH}")
    task = asyncio.create_task(_health_check_loop(bot))
    yield
    task.cancel()
    await bot.session.close()
    logging.info("Bot session closed")


app = FastAPI(lifespan=lifespan)


@app.post(WEBHOOK_PATH)
async def receive_update(update: Update, x_telegram_bot_api_secret_token: str = Header(default=None)):
    if x_telegram_bot_api_secret_token != SECRET_TOKEN:
        raise HTTPException(status_code=403)
    update = Update.model_validate(update)
    try:
        await dp.feed_update(bot, update)
    except Exception as e:
        logging.exception("Exception handled")
        logging.error(e)
        return {"ok": False}
    return {"ok": True}
