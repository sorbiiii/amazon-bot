import os
import re
import json
import time
import logging
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "@offerteamazonitalias")
INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))
MAX_PER_CYCLE = int(os.environ.get("MAX_PER_CYCLE", "5"))
SENT_FILE = Path("sent_offers.json")
ASIN_RE = re.compile(r"/dp/([A-Z0-9]{10})")

# Amazon.it category pages to harvest ASINs from
CATEGORY_PAGES = [
    "https://www.amazon.it/bestsellers/electronics",
    "https://www.amazon.it/bestsellers/computers",
    "https://www.amazon.it/bestsellers/kitchen",
    "https://www.amazon.it/bestsellers/sports",
    "https://www.amazon.it/gp/new-releases/electronics",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def load_sent() -> set:
    if SENT_FILE.exists():
        try:
            return set(json.loads(SENT_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_sent(sent: set) -> None:
    SENT_FILE.write_text(json.dumps(list(sent)))


def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 429:
            retry_after = r.json().get("parameters", {}).get("retry_after", 30)
            log.warning("Rate limited da Telegram, aspetto %ds", retry_after)
            time.sleep(retry_after)
            r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except requests.HTTPError as e:
        log.error("Telegram HTTP error: %s — %s", e, getattr(r, "text", ""))
    except Exception as e:
        log.error("Telegram send failed: %s", e)
    return False


def get_asins() -> list[str]:
    """Harvest ASINs from Amazon.it category/bestseller pages."""
    seen: set[str] = set()
    asins: list[str] = []
    for url in CATEGORY_PAGES:
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            found = ASIN_RE.findall(r.text)
            for asin in found:
                if asin not in seen:
                    seen.add(asin)
                    asins.append(asin)
            log.info("Trovati %d ASIN da %s", len(found), url.split("/")[-1])
        except Exception as e:
            log.error("Errore fetch categoria %s: %s", url, e)
        time.sleep(0.5)
    return asins


def scrape_product(asin: str) -> dict | None:
    """Scrape title, current price, and original price from an Amazon.it product page."""
    url = f"https://www.amazon.it/dp/{asin}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=6)
        soup = BeautifulSoup(r.text, "lxml")

        title_el = soup.select_one("#productTitle")
        price_w = soup.select_one("span.a-price-whole")
        price_f = soup.select_one("span.a-price-fraction")
        old_price_el = soup.select_one("span.a-price.a-text-price span.a-offscreen")
        badge_el = soup.select_one("span.savingPriceOverride, span.a-badge-text")

        if not title_el or not price_w:
            return None

        title = title_el.get_text(strip=True)
        price = price_w.get_text(strip=True).rstrip(",") + "," + (price_f.get_text(strip=True) if price_f else "00") + " €"
        old_price = old_price_el.get_text(strip=True) if old_price_el else None
        badge = badge_el.get_text(strip=True) if badge_el else None

        return {
            "asin": asin,
            "title": title,
            "price": price,
            "old_price": old_price,
            "badge": badge,
            "url": url,
        }
    except Exception as e:
        log.debug("Errore scraping ASIN %s: %s", asin, e)
        return None


def fetch_discounted_products() -> list[dict]:
    """Return only products that have a visible discount (old price shown)."""
    asins = get_asins()
    log.info("Totale ASIN da controllare: %d", len(asins))
    discounted = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(scrape_product, asin): asin for asin in asins}
        done = 0
        for future in as_completed(futures):
            done += 1
            result = future.result()
            if result and result["old_price"]:
                discounted.append(result)
                log.info("Sconto trovato [%d/%d]: %s — %s",
                         done, len(asins), result["asin"], result["title"][:50])
            elif done % 20 == 0:
                log.info("Progresso: %d/%d ASIN controllati, %d sconti trovati",
                         done, len(asins), len(discounted))
    log.info("Prodotti scontati trovati: %d", len(discounted))
    return discounted


def main() -> None:
    log.info("Bot avviato 🚀 — canale: %s, intervallo: %ds", CHANNEL, INTERVAL)
    sent = load_sent()

    if not sent:
        log.info("Prima esecuzione — pre-carico prodotti esistenti senza inviarli...")
        products = fetch_discounted_products()
        for p in products:
            sent.add(p["asin"])
        save_sent(sent)
        log.info("Pre-caricati %d prodotti. Le prossime offerte saranno inviate.", len(sent))
        time.sleep(INTERVAL)

    while True:
        try:
            products = fetch_discounted_products()
            new_count = 0
            for product in products:
                if new_count >= MAX_PER_CYCLE:
                    break
                if product["asin"] not in sent:
                    old = f"<s>{product['old_price']}</s> ➜ " if product["old_price"] else ""
                    badge = f" ({product['badge']})" if product["badge"] else ""
                    msg = (
                        f"🔥 <b>OFFERTA AMAZON</b>\n\n"
                        f"📦 <b>{product['title']}</b>\n\n"
                        f"💰 {old}<b>{product['price']}</b>{badge}\n"
                        f"📉 SCONTO ATTIVO\n\n"
                        f"👉 <a href='{product['url']}'>ACQUISTA SU AMAZON</a>\n"
                    )
                    if send_telegram(msg):
                        sent.add(product["asin"])
                        new_count += 1
                        time.sleep(2)
            save_sent(sent)
            log.info("Ciclo completato — %d nuove offerte inviate", new_count)
        except Exception as e:
            log.error("Errore nel ciclo principale: %s", e)

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
