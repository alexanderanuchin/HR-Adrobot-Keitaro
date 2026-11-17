"""Minimal aiogram service. Telegram user binding/monitoring will follow A-04."""
import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router()

@router.message(Command("start"))
async def start(message: Message) -> None:
    await message.answer("Adrobot: bot scaffold. Admin linking will be enabled later.")

@router.message(Command("status"))
async def status(message: Message) -> None:
    await message.answer("Adrobot bot online. Keitaro monitoring is not implemented yet.")

async def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing; keep telegram profile disabled until configured")
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    bot = Bot(token=token)
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
