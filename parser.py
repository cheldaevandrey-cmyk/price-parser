import csv
import time
import argparse
import requests
from datetime import datetime
from pathlib import Path

BASE_URL = "https://search.wb.ru/exactmatch/ru/common/v4/search"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
}


def fetch_page(query: str, page: int, session: requests.Session) -> list[dict]:
    params = {
        "appType": "1",
        "curr": "rub",
        "dest": "-1257786",
        "query": query,
        "resultset": "catalog",
        "sort": "popular",
        "spp": "30",
        "page": str(page),
    }
    try:
        resp = session.get(BASE_URL, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json().get("products") or []
    except Exception as e:
        print(f"  ⚠ Ошибка на странице {page}: {e}")
        return []


def parse_product(p: dict) -> dict | None:
    sizes = p.get("sizes") or []
    price_raw = sizes[0].get("price", {}).get("product") if sizes else None
    original_raw = sizes[0].get("price", {}).get("basic") if sizes else None

    price = round(price_raw / 100) if price_raw else None
    original = round(original_raw / 100) if original_raw else None

    if not price:
        return None

    product_id = p.get("id")
    return {
        "название": p.get("name", "").strip(),
        "бренд": p.get("brand", "").strip(),
        "цена": price,
        "цена_до_скидки": original,
        "рейтинг": p.get("reviewRating") or p.get("rating"),
        "отзывы": p.get("feedbacks", 0),
        "ссылка": f"https://www.wildberries.ru/catalog/{product_id}/detail.aspx",
    }


def run(query: str, pages: int, output: str) -> None:
    print(f"\n🔍 Запрос: «{query}» | Страниц: {pages}")

    session = requests.Session()
    session.headers.update(HEADERS)

    all_products = []
    for page in range(1, pages + 1):
        print(f"  Страница {page}/{pages}...", end=" ", flush=True)
        raw = fetch_page(query, page, session)
        if not raw:
            print("пусто, завершаем.")
            break

        parsed = [parse_product(p) for p in raw]
        valid = [p for p in parsed if p is not None]
        all_products.extend(valid)
        print(f"получено {len(valid)} товаров (всего: {len(all_products)})")

        if page < pages:
            time.sleep(0.5)

    if not all_products:
        print("❌ Товары не найдены.")
        return

    output_path = Path(output)
    fieldnames = ["название", "бренд", "цена", "цена_до_скидки", "рейтинг", "отзывы", "ссылка"]
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_products)

    print(f"\n✅ Сохранено {len(all_products)} товаров → {output_path.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Парсер цен с Wildberries.ru",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("query", help="Поисковый запрос, например: «ноутбук»")
    parser.add_argument(
        "-p", "--pages", type=int, default=3,
        help="Количество страниц (100 товаров на страницу)"
    )
    parser.add_argument(
        "-o", "--output", default="",
        help="Имя выходного CSV файла (по умолчанию: query_дата.csv)"
    )
    args = parser.parse_args()

    if not args.output:
        safe_query = args.query.replace(" ", "_")[:30]
        date_str = datetime.now().strftime("%Y%m%d_%H%M")
        args.output = f"{safe_query}_{date_str}.csv"

    run(args.query, args.pages, args.output)


if __name__ == "__main__":
    main()
