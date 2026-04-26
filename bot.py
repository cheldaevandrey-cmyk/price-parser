import io
import csv
import json
import logging
import os
import time
import asyncio
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile,
)

from parser import fetch_page, parse_product

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BOT_TOKEN = os.environ["BOT_TOKEN"]
MONITORS_FILE = os.path.join(os.path.dirname(__file__), "monitors.json")
DIGEST_STATE_FILE = os.path.join(os.path.dirname(__file__), "digest_state.json")
NOTIFY_COOLDOWN = 3600
DIGEST_HOUR = 9
DIGEST_TZ = ZoneInfo("Asia/Yekaterinburg")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

_pending: dict[str, list[dict]] = {}


class WatchStates(StatesGroup):
    waiting_price = State()


# ── Storage ───────────────────────────────────────────────────────────────────

def load_monitors() -> list[dict]:
    try:
        with open(MONITORS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_monitors(monitors: list[dict]) -> None:
    with open(MONITORS_FILE, "w", encoding="utf-8") as f:
        json.dump(monitors, f, ensure_ascii=False, indent=2)


def load_digest_state() -> dict:
    try:
        with open(DIGEST_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_digest_state(state: dict) -> None:
    with open(DIGEST_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ── WB helpers ────────────────────────────────────────────────────────────────

CATEGORIES = {
    "iphone_14_17": {
        "label": "📱 iPhone 14–17",
        "title": "Лучшие цены на iPhone 14–17",
        "models": [
            "iPhone 14", "iPhone 14 Pro", "iPhone 14 Pro Max",
            "iPhone 15", "iPhone 15 Pro", "iPhone 15 Pro Max",
            "iPhone 16", "iPhone 16 Pro", "iPhone 16 Pro Max",
            "iPhone 17", "iPhone 17 Pro", "iPhone 17 Pro Max",
        ],
    },
}

CATEGORIES_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text=cat["label"], callback_data=f"cat:{key}")]
    for key, cat in CATEGORIES.items()
])

_USED_KEYWORDS = ("б/у", "бу ", "used", "восстановл", "refurbished", "ремонт", "копия", "реплика")


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Origin": "https://www.wildberries.ru",
        "Referer": "https://www.wildberries.ru/",
    })
    return s


def is_new(product: dict) -> bool:
    return not any(kw in product["название"].lower() for kw in _USED_KEYWORDS)


def search_products(query: str, session: requests.Session, limit: int = 25) -> list[dict]:
    raw = fetch_page(query, page=1, session=session)
    return [p for r in raw if (p := parse_product(r)) is not None][:limit]


def best_per_model(models: list[str], session: requests.Session) -> list[dict]:
    results = []
    for model in models:
        products = [p for p in search_products(model, session, limit=25) if is_new(p)]
        if products:
            cheapest = min(products, key=lambda p: p["цена"])
            cheapest["_model"] = model
            results.append(cheapest)
    return results


def get_usd_rate() -> float | None:
    try:
        resp = requests.get("https://www.cbr-xml-daily.ru/daily_json.js", timeout=10)
        resp.raise_for_status()
        return resp.json()["Valute"]["USD"]["Value"]
    except Exception as e:
        logging.error(f"CBR fetch error: {e}")
        return None


# ── Formatters ────────────────────────────────────────────────────────────────

def format_category(title: str, rows: list[dict]) -> str:
    lines = [f"📱 <b>{title}</b>\n"]
    for p in rows:
        price_str = f"{p['цена']:,} ₽".replace(",", " ")
        old_price = p.get("цена_до_скидки")
        discount = f" <s>{old_price:,} ₽</s>".replace(",", " ") if old_price and old_price > p["цена"] else ""
        rating = f"⭐ {p['рейтинг']}" if p.get("рейтинг") else ""
        lines.append(
            f"<b>{p['_model']}</b> — {price_str}{discount}\n"
            f"  {p['название']}\n"
            f"  {rating}\n"
            f"  <a href=\"{p['ссылка']}\">Открыть на WB</a>\n"
        )
    return "\n".join(lines)


def format_results(title: str, products: list[dict]) -> str:
    lines = [f"🔍 <b>{title}</b> — {len(products)} товаров\n"]
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


async def send_results(target: Message, title: str, products: list[dict], cache_key: str) -> None:
    _pending[cache_key] = products
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Скачать CSV", callback_data=f"csv:{cache_key}")],
        [InlineKeyboardButton(text="🔔 Следить за ценой", callback_data=f"watch:{cache_key}")],
    ])
    await target.answer(
        format_results(title, products),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


# ── Daily digest ──────────────────────────────────────────────────────────────

async def send_daily_digest(user_ids: list[int] | None = None) -> None:
    monitors = load_monitors()
    if not monitors:
        return

    session = make_session()
    usd = get_usd_rate()
    usd_line = f"💵 Доллар: <b>{usd:.2f} ₽</b>\n" if usd else ""
    today = datetime.now(DIGEST_TZ).strftime("%d.%m.%Y")

    # group by user
    by_user: dict[int, list[dict]] = {}
    for m in monitors:
        uid = m["user_id"]
        if user_ids and uid not in user_ids:
            continue
        by_user.setdefault(uid, []).append(m)

    digest_state = load_digest_state()

    for uid, user_monitors in by_user.items():
        lines = [
            f"🌅 <b>Доброе утро! Сводка на {today}</b>\n",
            usd_line,
            "📦 <b>Твои мониторы:</b>\n",
        ]
        monitors_updated = False

        for m in user_monitors:
            try:
                products = [p for p in search_products(m["query"], session, limit=25) if is_new(p)]
                if not products:
                    lines.append(f"• <b>{m['query']}</b> — нет результатов\n")
                    continue

                cheapest = min(products, key=lambda p: p["цена"])
                current = cheapest["цена"]
                last = m.get("last_price")
                threshold_str = f"{m['threshold']:,} ₽".replace(",", " ")
                price_str = f"{current:,} ₽".replace(",", " ")

                if last is None:
                    change = ""
                elif current < last:
                    diff = f"{last - current:,}".replace(",", " ")
                    change = f" ↓ <b>−{diff} ₽</b>"
                elif current > last:
                    diff = f"{current - last:,}".replace(",", " ")
                    change = f" ↑ +{diff} ₽"
                else:
                    change = " — без изменений"

                alert = " 🔥" if current <= m["threshold"] else ""
                lines.append(
                    f"• <b>{m['query']}</b>{alert}\n"
                    f"  💰 {price_str}{change}\n"
                    f"  Порог: {threshold_str} · <a href=\"{cheapest['ссылка']}\">WB</a>\n"
                )

                m["last_price"] = current
                monitors_updated = True

            except Exception as e:
                logging.error(f"Digest fetch error ({m['query']}): {e}")
                lines.append(f"• <b>{m['query']}</b> — ошибка запроса\n")

        try:
            await bot.send_message(
                uid,
                "\n".join(lines),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            digest_state[str(uid)] = today
            logging.info(f"Digest sent to user {uid}")
        except Exception as e:
            logging.error(f"Digest send error (user {uid}): {e}")

        if monitors_updated:
            save_monitors(monitors)

    save_digest_state(digest_state)


async def digest_scheduler() -> None:
    while True:
        await asyncio.sleep(30)
        now = datetime.now(DIGEST_TZ)
        if now.hour != DIGEST_HOUR or now.minute != 0:
            continue

        today = now.strftime("%d.%m.%Y")
        digest_state = load_digest_state()
        monitors = load_monitors()
        user_ids = list({m["user_id"] for m in monitors})

        pending_users = [uid for uid in user_ids if digest_state.get(str(uid)) != today]
        if pending_users:
            logging.info(f"Sending digest to {len(pending_users)} users")
            await send_daily_digest(pending_users)

        await asyncio.sleep(60)


# ── Handlers ──────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Привет! Я ищу товары на <b>Wildberries</b> и показываю цены.\n\n"
        "Напиши название товара или выбери категорию:\n\n"
        "📋 /watches — активные мониторы\n"
        "📊 /digest — получить сводку прямо сейчас",
        parse_mode="HTML",
        reply_markup=CATEGORIES_KEYBOARD,
    )


@dp.message(Command("watches"))
async def cmd_watches(message: Message) -> None:
    monitors = [m for m in load_monitors() if m["user_id"] == message.from_user.id]
    if not monitors:
        await message.answer("У тебя нет активных мониторов.")
        return

    lines = ["📋 <b>Активные мониторы:</b>\n"]
    keyboard_rows = []
    for m in monitors:
        threshold_str = f"{m['threshold']:,} ₽".replace(",", " ")
        last = m.get("last_price")
        last_str = f" (сейчас {last:,} ₽)".replace(",", " ") if last else ""
        lines.append(f"• <b>{m['query']}</b> — ниже {threshold_str}{last_str}")
        keyboard_rows.append([
            InlineKeyboardButton(text=f"❌ {m['query']}", callback_data=f"unwatch:{m['id']}"),
        ])

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )


@dp.message(Command("digest"))
async def cmd_digest(message: Message) -> None:
    status = await message.answer("⏳ Собираю сводку...")
    await send_daily_digest(user_ids=[message.from_user.id])
    await status.delete()


@dp.callback_query(F.data.startswith("watch:"))
async def handle_watch(callback: CallbackQuery, state: FSMContext) -> None:
    cache_key = callback.data.removeprefix("watch:")
    query = cache_key.split(":", 1)[1]
    await state.set_state(WatchStates.waiting_price)
    await state.update_data(query=query)
    await callback.answer()
    await callback.message.answer(
        f"🔔 Слежу за: <b>{query}</b>\n\nПри какой цене уведомить? Введи сумму в рублях:",
        parse_mode="HTML",
    )


@dp.message(WatchStates.waiting_price)
async def handle_watch_price(message: Message, state: FSMContext) -> None:
    text = message.text.strip().replace(" ", "").replace("₽", "")
    if not text.isdigit():
        await message.answer("Введи число, например: <code>70000</code>", parse_mode="HTML")
        return

    threshold = int(text)
    data = await state.get_data()
    query = data["query"]

    monitors = load_monitors()
    monitors.append({
        "id": str(uuid.uuid4())[:8],
        "user_id": message.from_user.id,
        "query": query,
        "threshold": threshold,
        "last_notified": 0,
        "last_price": None,
    })
    save_monitors(monitors)
    await state.clear()

    threshold_str = f"{threshold:,} ₽".replace(",", " ")
    await message.answer(
        f"✅ Монитор установлен!\n\n"
        f"📦 <b>{query}</b>\n"
        f"💰 Уведомлю когда цена упадёт ниже <b>{threshold_str}</b>\n"
        f"🌅 Ежедневная сводка в 9:00 по Екатеринбургу\n\n"
        f"Управление: /watches",
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("unwatch:"))
async def handle_unwatch(callback: CallbackQuery) -> None:
    monitor_id = callback.data.removeprefix("unwatch:")
    monitors = [m for m in load_monitors() if m["id"] != monitor_id]
    save_monitors(monitors)
    await callback.answer("Монитор удалён.", show_alert=True)
    await callback.message.delete()


@dp.callback_query(F.data.startswith("cat:"))
async def handle_category(callback: CallbackQuery) -> None:
    cat_key = callback.data.removeprefix("cat:")
    cat = CATEGORIES.get(cat_key)
    if not cat:
        await callback.answer("Категория не найдена.", show_alert=True)
        return

    await callback.answer()
    status = await callback.message.answer(
        f"⏳ Собираю лучшие цены: {cat['label']}...", parse_mode="HTML"
    )

    try:
        session = make_session()
        products = best_per_model(cat["models"], session)
    except Exception as e:
        logging.error(f"Category fetch error: {e}")
        await status.edit_text("❌ Ошибка при запросе к Wildberries. Попробуй ещё раз.")
        return

    if not products:
        await status.edit_text(f"😕 Ничего не найдено по категории {cat['label']}.")
        return

    cache_key = f"{callback.from_user.id}:{cat_key}"
    _pending[cache_key] = products

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📥 Скачать CSV", callback_data=f"csv:{cache_key}"),
    ]])
    await status.delete()
    await callback.message.answer(
        format_category(cat["title"], products),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


@dp.message(F.text)
async def handle_query(message: Message) -> None:
    query = message.text.strip()
    status = await message.answer(f"⏳ Ищу <b>{query}</b>...", parse_mode="HTML")

    try:
        session = make_session()
        products = sorted(
            search_products(query, session),
            key=lambda p: p["цена"],
        )[:10]
    except Exception as e:
        logging.error(f"WB fetch error: {e}")
        await status.edit_text("❌ Ошибка при запросе к Wildberries. Попробуй ещё раз.")
        return

    if not products:
        await status.edit_text(f"😕 По запросу <b>{query}</b> ничего не найдено.", parse_mode="HTML")
        return

    await status.delete()
    cache_key = f"{message.from_user.id}:{query}"
    await send_results(message, query, products, cache_key)


@dp.callback_query(F.data.startswith("csv:"))
async def send_csv(callback: CallbackQuery) -> None:
    cache_key = callback.data.removeprefix("csv:")
    products = _pending.get(cache_key)

    if not products:
        await callback.answer("Данные устарели, повтори запрос.", show_alert=True)
        return

    label = cache_key.split(":", 1)[1]
    safe_name = label.replace(" ", "_")[:40]

    csv_bytes = make_csv_bytes(products)
    await callback.message.answer_document(
        BufferedInputFile(csv_bytes, filename=f"{safe_name}.csv"),
        caption=f"📊 {len(products)} товаров — {label}",
    )
    await callback.answer()


# ── Price monitor background task ─────────────────────────────────────────────

async def price_monitor() -> None:
    session = make_session()
    index = 0

    while True:
        await asyncio.sleep(1)

        monitors = load_monitors()
        if not monitors:
            continue

        m = monitors[index % len(monitors)]
        index += 1

        try:
            products = [p for p in search_products(m["query"], session, limit=25) if is_new(p)]
            if not products:
                continue

            cheapest = min(products, key=lambda p: p["цена"])
            current = cheapest["цена"]

            m["last_price"] = current
            save_monitors(monitors)

            if current <= m["threshold"]:
                now = time.time()
                if now - m.get("last_notified", 0) < NOTIFY_COOLDOWN:
                    continue

                price_str = f"{current:,} ₽".replace(",", " ")
                threshold_str = f"{m['threshold']:,} ₽".replace(",", " ")
                await bot.send_message(
                    m["user_id"],
                    f"🔔 <b>Цена упала!</b>\n\n"
                    f"📦 {cheapest['название']}\n"
                    f"💰 <b>{price_str}</b> (порог: {threshold_str})\n"
                    f"🔗 <a href=\"{cheapest['ссылка']}\">Открыть на WB</a>",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )

                m["last_notified"] = now
                save_monitors(monitors)
                logging.info(f"Alert sent: {m['query']} → {current} ₽ (user {m['user_id']})")

        except Exception as e:
            logging.error(f"Monitor error ({m['query']}): {e}")


async def main() -> None:
    logging.info("Bot started")
    asyncio.create_task(price_monitor())
    asyncio.create_task(digest_scheduler())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
