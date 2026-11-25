import logging
import os
from functools import wraps
from typing import Any, Callable, Optional

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

load_dotenv()

engine = create_async_engine(
    os.getenv("DB_LINK_CONNECTION"),
    pool_pre_ping=True,
    pool_recycle=28000,
    hide_parameters=True,  # щоб текст повідомлень не потрапляв у логи помилок
    connect_args={"timeout": 3, "command_timeout": 30},
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def log_exc_with_try_except():
    def decorator(func: Callable) -> Callable:

        @wraps(func)
        async def wrapper(*args, **kwargs) -> Optional[Any]:
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logging.error(f"Error in {func.__name__}: {e}")
                return None

        return wrapper

    return decorator
