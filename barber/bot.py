import os, asyncio
from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, MenuButtonWebApp

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]
if not WEBAPP_URL.endswith('/'): WEBAPP_URL += '/'
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def start(m: Message):
    user_id = m.from_user.id
    if user_id in ADMIN_IDS:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📅 Открыть админку", web_app=WebAppInfo(url=WEBAPP_URL))]])
        await m.answer("Привет, Барбер!", reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✂️ Записаться", web_app=WebAppInfo(url=WEBAPP_URL + "client"))],
            [InlineKeyboardButton(text="📋 Мои записи", web_app=WebAppInfo(url=WEBAPP_URL + "client?view=my"))],
            [InlineKeyboardButton(text="⭐ Оставить отзыв", web_app=WebAppInfo(url=WEBAPP_URL + "client?view=reviews"))]
        ])
        await m.answer("Привет! Выбери действие:", reply_markup=kb)

async def main():
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Открыть", web_app=WebAppInfo(url=WEBAPP_URL)))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())