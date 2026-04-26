import io
import csv
import logging
import os
import requests
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile,
)
from aiogram.fsm.storage.memory import MemoryStorage
import asyncio

from parser import fetch_page, parse_product

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BOT_TOKEN = os.environ["BOT_TOKEN"]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

_pending: dict[str, list[dict]] = {}


def format_results(query: str, products: list[dict]) -> str:
    lines = [f"🔍 <b>{query}</b> — найдено {len(products)} товаров\n"]
    for i, p in enumerate(products, 1):
        price_str = f"{p['цена']:,} ₽".replace(",", " ")
        old_price = p.get("цена_до_скидки")
        discount = f" <s>{old_price:,} ₽</s>".replace(",", " ") if old_price and old_price > p["цена"] else ""
        rating = f"⭐ {p['рейтинг']}" if p.get("рейтинг") else ""
        reviews = f" · {p['отзывы']:,} отз.".replace(",", " ") if p.get("отзывы") else ""
        lines.append(
            f"<b>{i}. {p['название']}</b>\n"
            f"   💰 {price_str}{discount}\n"
            f"   {rating}{reviews}\n"
            f"   <a href=\"{p['ссылка']}\">Открыть на WB</a>\n"
        )
    return "\n".join(lines)


def make_csv_bytes(products: list[dict]) -> bytes:
    buf = io.StringIO()
    fieldnames = ["название", "бренд", "цена", "цена_до_скидки", "рейтинг", "отзывы", "ссылка"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(products)
    return buf.getvalue().encode("utf-8-sig")


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Привет! Я ищу товары на <b>Wildberries</b> и показываю цены.\n\n"
        "Просто напиши название товара, например:\n"
        "  <code>iPhone 16 Pro 128gb</code>\n"
        "  <code>кроссовки Nike Air Max</code>\n"
        "  <code>наушники Sony WH-1000XM5</code>",
        parse_mode="HTML",
    )


@dp.message(F.text)
async def handle_query(message: Message) -> None:
    query = message.text.strip()
    status = await message.answer(f"⏳ Ищу <b>{query}</b>...", parse_mode="HTML")

    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "*/*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Origin": "https://www.wildberries.ru",
            "Referer": "https://www.wildberries.ru/",
        })
        raw = fetch_page(query, page=1, session=session)
        products = [p for r in raw if (p := parse_product(r)) is not None][:10]
    except Exception as e:
        logging.error(f"WB fetch error: {e}")
        await status.edit_text("❌ Ошибка при запросе к Wildberries. Попробуй ещё раз.")
        return

    if not products:
        await status.edit_text(f"😕 По запросу <b>{query}</b> ничего не найдено.", parse_mode="HTML")
        return

    cache_key = f"{message.from_user.id}:{query}"
    _pending[cache_key] = products

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📥 Скачать CSV", callback_data=f"csv:{cache_key}"),
    ]])

    await status.delete()
    await message.answer(
        format_results(query, products),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


@dp.callback_query(F.data.startswith("csv:"))
async def send_csv(callback: CallbackQuery) -> None:
    cache_key = callback.data.removeprefix("csv:")
    products = _pending.get(cache_key)

    if not products:
        await callback.answer("Данные устарели, повтори запрос.", show_alert=True)
        return

    query = cache_key.split(":", 1)[1]
    safe_name = query.replace(" ", "_")[:40]
    filename = f"{safe_name}.csv"

    csv_bytes = make_csv_bytes(products)
    await callback.message.answer_document(
        BufferedInputFile(csv_bytes, filename=filename),
        caption=f"📊 {len(products)} товаров по запросу «{query}»",
    )
    await callback.answer()


async def main() -> None:
    logging.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
