#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║       DealPulses — Direct Merchant Scraper                       ║
║  Fetches live deals straight from retailer websites/APIs.        ║
║  No aggregator middleman — every URL points to the real store.   ║
║                                                                  ║
║  Merchants covered:                                              ║
║    • Amazon        (Gold Box / Today's Deals)                    ║
║    • Best Buy      (Deal of the Day / Sale)                      ║
║    • Newegg        (Daily Deals)                                 ║
║    • B&H Photo     (Hot Deals)                                   ║
║    • Dell          (Outlet + Promotions)                         ║
║    • Woot!         (already via RSS in dealradar.py)             ║
║                                                                  ║
║  Merges results into the shared dealradar.db and regenerates     ║
║  deals.json alongside dealradar.py (runs in parallel in CI).    ║
║                                                                  ║
║  Usage:                                                          ║
║    python merchant_scraper.py            # Run all scrapers      ║
║    python merchant_scraper.py --top 10   # Print top 10 only     ║
╚══════════════════════════════════════════════════════════════════╝
"""

# ─────────────────────────────────────────────────────────────────
#  CONFIG  — must match dealradar.py paths
# ─────────────────────────────────────────────────────────────────
DB_PATH      = "dealradar.db"
LOG_PATH     = "merchant_scraper.log"
EXPORT_DIR   = "."               # Where deals.json lives (site root)
MAX_AGE_DAYS = 3                 # Ignore deals older than this
REQUEST_TIMEOUT = 8              # Seconds per HTTP request
MAX_WORKERS  = 10                # Parallel threads for image fetching

AMAZON_AFFILIATE_TAG = "dealpulses0e-20"   # Amazon Associates tag

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

# ─────────────────────────────────────────────────────────────────
#  IMPORTS
# ─────────────────────────────────────────────────────────────────
import re
import sys
import json
import time
import math
import hashlib
import logging
import sqlite3
import datetime
import argparse
import os
from urllib.parse import urlparse, urlencode, urlunparse, parse_qs, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Run: pip install requests beautifulsoup4")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("MerchantScraper")


# ─────────────────────────────────────────────────────────────────
#  DATABASE  (same schema as dealradar.py)
# ─────────────────────────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def ensure_schema():
    """Make sure the deals table exists (in case dealradar hasn't run yet)."""
    con = get_db()
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS deals (
            id          TEXT PRIMARY KEY,
            title       TEXT,
            url         TEXT,
            source      TEXT,
            category    TEXT,
            score       INTEGER,
            price_now   REAL,
            price_was   REAL,
            discount    REAL,
            summary     TEXT,
            image_url   TEXT DEFAULT '',
            alerted     INTEGER DEFAULT 0,
            first_seen  TEXT,
            last_seen   TEXT
        )
    """)
    for col_sql in [
        "ALTER TABLE deals ADD COLUMN image_url  TEXT DEFAULT ''",
        "ALTER TABLE deals ADD COLUMN status     TEXT DEFAULT 'active'",
        "ALTER TABLE deals ADD COLUMN checked_at TEXT DEFAULT ''",
    ]:
        try:
            cur.execute(col_sql)
        except Exception:
            pass
    cur.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            deal_id  TEXT,
            price    REAL,
            date     TEXT,
            PRIMARY KEY (deal_id, date)
        )
    """)
    con.commit()
    con.close()


def deal_id(title: str, url: str) -> str:
    return hashlib.md5(f"{title}|{url}".encode()).hexdigest()[:16]


def upsert_deal(cur, deal: dict):
    """Insert or update a deal row. Updates score / prices / image on conflict."""
    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    cur.execute("""
        INSERT INTO deals
            (id, title, url, source, category, score,
             price_now, price_was, discount, summary,
             image_url, alerted, first_seen, last_seen, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?,'active')
        ON CONFLICT(id) DO UPDATE SET
            score     = MAX(score,    excluded.score),
            price_now = COALESCE(excluded.price_now, price_now),
            price_was = COALESCE(excluded.price_was, price_was),
            discount  = COALESCE(excluded.discount,  discount),
            image_url = CASE WHEN excluded.image_url != ''
                             THEN excluded.image_url
                             ELSE image_url END,
            last_seen = excluded.last_seen,
            status    = 'active'
    """, (
        deal["id"], deal["title"], deal["url"], deal["source"],
        deal.get("category", "tech"), deal.get("score", 50),
        deal.get("price_now"), deal.get("price_was"), deal.get("discount"),
        deal.get("summary", ""), deal.get("image_url", ""),
        deal.get("first_seen", now), now,
    ))


# ─────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────
def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lstrip("www.")
    except Exception:
        return ""


def _price(text) -> float | None:
    """Extract first dollar amount from a string, e.g. '$1,299.99' → 1299.99"""
    if not text:
        return None
    text = str(text)
    m = re.search(r"\$?([\d,]+\.?\d*)", text.replace(",", ""))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _discount_pct(now: float | None, was: float | None) -> float | None:
    if now and was and was > now:
        return round((was - now) / was * 100, 1)
    return None


def _score(price_now, price_was, discount_pct, title=""):
    """Simple scoring — mirrors dealradar.py logic (no email alerts here)."""
    score = 40  # base
    if discount_pct:
        score += min(int(discount_pct * 0.8), 40)   # up to +40
    if price_was and price_now and price_was - price_now >= 100:
        score += 10
    lowered = title.lower() if title else ""
    for kw, bonus in [("all-time low", 25), ("lowest price", 20),
                       ("flash sale", 15), ("lightning deal", 15),
                       ("today only", 12), ("limited time", 10)]:
        if kw in lowered:
            score += bonus
            break
    return min(score, 99)


def add_affiliate_tag(url: str) -> str:
    """Append Amazon Associates tag to amazon.com URLs."""
    if "amazon.com" not in url:
        return url
    # Remove existing tag to avoid duplicates
    url = re.sub(r'[?&]tag=[^&]+', '', url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}tag={AMAZON_AFFILIATE_TAG}"


def fetch_image(url: str, timeout: int = REQUEST_TIMEOUT) -> str:
    """Fetch a product image from a direct merchant URL."""
    if not url:
        return ""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout,
                            allow_redirects=True)
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        host = _host(resp.url)

        # Amazon — extract hi-res from ASIN page
        if "amazon.com" in host:
            tag = soup.find(id="landingImage") or soup.find(id="imgBlkFront")
            if tag:
                dyn = tag.get("data-a-dynamic-image", "")
                if dyn:
                    try:
                        imgs = json.loads(dyn)
                        best = max(imgs.items(), key=lambda kv: kv[1][0])[0]
                        return best
                    except Exception:
                        pass
                src = tag.get("src", "")
                if src and src.startswith("http"):
                    return re.sub(r'\._[A-Z0-9_,]+_\.', '.', src)

        # og:image — universal fallback
        og = (soup.find("meta", property="og:image") or
              soup.find("meta", attrs={"name": "og:image"}))
        if og:
            content = og.get("content", "").strip()
            if content.startswith("http"):
                return content

        # twitter:image
        tw = (soup.find("meta", attrs={"name": "twitter:image"}) or
              soup.find("meta", property="twitter:image"))
        if tw:
            content = tw.get("content", "").strip()
            if content.startswith("http"):
                return content

    except Exception as e:
        log.debug(f"Image fetch failed for {url}: {e}")
    return ""


# ─────────────────────────────────────────────────────────────────
#  SCRAPERS
# ─────────────────────────────────────────────────────────────────

# ── 1. AMAZON — Gold Box / Today's Deals ─────────────────────────
def scrape_amazon() -> list[dict]:
    """
    Fetch Amazon's Gold Box deals via their internal JSON API.
    Returns direct amazon.com product URLs with affiliate tag.
    """
    log.info("🛒 Amazon: fetching Gold Box deals…")
    deals = []

    # Amazon's internal deals API — returns JSON with deal cards
    api_url = (
        "https://www.amazon.com/gp/deals/ajax/getGoldboxDeals"
        "?page=1&paginationToken=&refTagPrefix=GB_T1_"
        "&deviceType=desktop&swrve_item_count=0&isPrime=false"
    )
    hdrs = {**HEADERS,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.amazon.com/deals"}
    try:
        resp = requests.get(api_url, headers=hdrs, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            raise ValueError(f"HTTP {resp.status_code}")
        data = resp.json()
    except Exception as e:
        log.warning(f"  Amazon API failed ({e}), trying HTML fallback…")
        return _scrape_amazon_html()

    # Parse deal cards from JSON response
    deal_data = data.get("dealDetails", {})
    if not deal_data:
        log.warning("  Amazon: no dealDetails in response, trying HTML…")
        return _scrape_amazon_html()

    for deal_id_str, d in deal_data.items():
        try:
            title    = d.get("title", "").strip()
            if not title:
                continue
            asin     = d.get("asin", "")
            price_n  = _price(d.get("dealPrice", d.get("currencyCode", "")))
            price_w  = _price(d.get("originalPrice", ""))
            disc     = _discount_pct(price_n, price_w) or d.get("percentOff")
            img_url  = (d.get("primaryImage", "") or
                        d.get("imageUrl", "")).strip()
            url      = (
                f"https://www.amazon.com/dp/{asin}?tag={AMAZON_AFFILIATE_TAG}"
                if asin else ""
            )
            if not url:
                continue

            did = deal_id(title, url)
            deals.append({
                "id": did, "title": title, "url": url,
                "source": "Amazon", "category": "tech",
                "score": _score(price_n, price_w, disc, title),
                "price_now": price_n, "price_was": price_w,
                "discount": disc, "summary": title,
                "image_url": img_url,
                "first_seen": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
            })
        except Exception as e:
            log.debug(f"  Amazon deal parse error: {e}")

    log.info(f"  Amazon: {len(deals)} deals from API")
    return deals


def _scrape_amazon_html() -> list[dict]:
    """HTML fallback — parse Amazon's Today's Deals page."""
    log.info("  Amazon: trying HTML scrape of /deals…")
    deals = []
    try:
        resp = requests.get("https://www.amazon.com/deals",
                            headers=HEADERS, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Deal cards are in <div data-hook="dealCard"> or <div data-testid="...">
        cards = (soup.find_all("div", attrs={"data-testid": re.compile(r"deal-card")}) or
                 soup.find_all("div", attrs={"data-hook": "dealCard"}))

        for card in cards[:30]:
            try:
                # Title
                t_el = (card.find("span", attrs={"data-hook": "deal-title"}) or
                        card.find("a", class_=re.compile(r"title")))
                title = t_el.get_text(strip=True) if t_el else ""
                if not title:
                    continue

                # URL
                a_el = card.find("a", href=True)
                href = a_el["href"] if a_el else ""
                if not href:
                    continue
                url = urljoin("https://www.amazon.com", href)
                url = add_affiliate_tag(url)

                # Prices
                price_el = card.find(attrs={"data-hook": re.compile(r"price")})
                price_n = _price(price_el.get_text()) if price_el else None

                # Image
                img_el = card.find("img")
                img_url = img_el.get("src", "") if img_el else ""

                did = deal_id(title, url)
                deals.append({
                    "id": did, "title": title, "url": url,
                    "source": "Amazon", "category": "tech",
                    "score": _score(price_n, None, None, title),
                    "price_now": price_n, "price_was": None,
                    "discount": None, "summary": title,
                    "image_url": img_url,
                    "first_seen": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
                })
            except Exception:
                pass

    except Exception as e:
        log.warning(f"  Amazon HTML scrape failed: {e}")

    log.info(f"  Amazon HTML: {len(deals)} deals")
    return deals


# ── 2. BEST BUY — Deal of the Day ────────────────────────────────
def scrape_bestbuy() -> list[dict]:
    """
    Fetch Best Buy deals via their internal offers API.
    Falls back to scraping their Deals page.
    """
    log.info("🛒 Best Buy: fetching deals…")
    deals = []

    # Best Buy's internal category deals API
    api_url = (
        "https://www.bestbuy.com/api/tcfm/v1/tm/offers/"
        "bestbuy-us-sitecache/deals/en_US/all?start=0&limit=24&sort=PRICE_REDUCTION_AMOUNT"
    )
    try:
        resp = requests.get(api_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            raise ValueError(f"HTTP {resp.status_code}")
        data = resp.json()
        items = data.get("offers", data.get("data", []))
        if not items:
            raise ValueError("empty response")
    except Exception as e:
        log.warning(f"  Best Buy API failed ({e}), trying HTML…")
        return _scrape_bestbuy_html()

    for item in items[:30]:
        try:
            title   = item.get("name", item.get("title", "")).strip()
            if not title:
                continue
            sku     = item.get("sku", "")
            price_n = _price(item.get("salePrice", item.get("discountedPrice")))
            price_w = _price(item.get("regularPrice", item.get("originalPrice")))
            disc    = _discount_pct(price_n, price_w)
            img_url = item.get("images", {}).get("standard", "")
            if not img_url:
                img_url = item.get("thumbnailImage", "")
            url     = (f"https://www.bestbuy.com/site/-/{sku}.p"
                       if sku else item.get("url", ""))
            if not url:
                continue

            did = deal_id(title, url)
            deals.append({
                "id": did, "title": title, "url": url,
                "source": "Best Buy", "category": "tech",
                "score": _score(price_n, price_w, disc, title),
                "price_now": price_n, "price_was": price_w,
                "discount": disc, "summary": title,
                "image_url": img_url,
                "first_seen": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
            })
        except Exception as e:
            log.debug(f"  Best Buy item error: {e}")

    log.info(f"  Best Buy: {len(deals)} deals from API")
    return deals


def _scrape_bestbuy_html() -> list[dict]:
    """HTML fallback for Best Buy deals page."""
    log.info("  Best Buy: trying HTML scrape…")
    deals = []
    try:
        resp = requests.get(
            "https://www.bestbuy.com/site/electronics/todays-deals/pcmcat248000050016.c",
            headers=HEADERS, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(resp.text, "html.parser")

        for card in soup.select("li.sku-item")[:30]:
            try:
                title_el = card.select_one("h4.sku-title a")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                href  = title_el.get("href", "")
                url   = urljoin("https://www.bestbuy.com", href)

                price_n_el = card.select_one(".priceView-customer-price span")
                price_w_el = card.select_one(".pricing-price__regular-price")
                price_n = _price(price_n_el.get_text()) if price_n_el else None
                price_w = _price(price_w_el.get_text()) if price_w_el else None
                disc    = _discount_pct(price_n, price_w)

                img_el  = card.select_one("img.product-image")
                img_url = img_el.get("src", "") if img_el else ""

                did = deal_id(title, url)
                deals.append({
                    "id": did, "title": title, "url": url,
                    "source": "Best Buy", "category": "tech",
                    "score": _score(price_n, price_w, disc, title),
                    "price_now": price_n, "price_was": price_w,
                    "discount": disc, "summary": title,
                    "image_url": img_url,
                    "first_seen": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
                })
            except Exception:
                pass
    except Exception as e:
        log.warning(f"  Best Buy HTML failed: {e}")

    log.info(f"  Best Buy HTML: {len(deals)} deals")
    return deals


# ── 3. NEWEGG — Daily Deals ──────────────────────────────────────
def scrape_newegg() -> list[dict]:
    """
    Fetch Newegg daily deals via their JSON API endpoint.
    """
    log.info("🛒 Newegg: fetching daily deals…")
    deals = []

    api_url = "https://www.newegg.com/api/2/page/nested/dailydeals"
    try:
        resp = requests.get(api_url, headers={**HEADERS, "Accept": "application/json"},
                            timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            raise ValueError(f"HTTP {resp.status_code}")
        data = resp.json()
    except Exception as e:
        log.warning(f"  Newegg API failed ({e}), trying HTML…")
        return _scrape_newegg_html()

    # Newegg API response structure: data.MainContent[].items[] or similar
    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("Items", "items", "Products", "products", "Deals", "deals"):
            if key in data and isinstance(data[key], list):
                items = data[key]
                break
        if not items:
            # Dig one level deeper
            for v in data.values():
                if isinstance(v, dict):
                    for key in ("Items", "items", "Products", "products"):
                        if key in v and isinstance(v[key], list):
                            items = v[key]
                            break
                elif isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict):
                    items = v
                    break

    if not items:
        log.warning("  Newegg: couldn't parse API response, trying HTML…")
        return _scrape_newegg_html()

    for item in items[:30]:
        try:
            title   = (item.get("Description", item.get("Name", "")) or "").strip()
            if not title:
                continue
            item_no = item.get("ItemNumber", item.get("NeweggItemNumber", ""))
            url     = (item.get("Url", item.get("ProductUrl", "")) or
                       (f"https://www.newegg.com/p/{item_no}" if item_no else ""))
            if not url:
                continue
            if not url.startswith("http"):
                url = "https://www.newegg.com" + url

            price_n = _price(item.get("FinalPrice", item.get("SalePrice")))
            price_w = _price(item.get("OriginalPrice", item.get("RegularPrice")))
            disc    = _discount_pct(price_n, price_w) or item.get("SavePercent")
            img_url = item.get("ThumbnailUrl", item.get("Image", "")) or ""

            did = deal_id(title, url)
            deals.append({
                "id": did, "title": title, "url": url,
                "source": "Newegg", "category": "tech",
                "score": _score(price_n, price_w, disc, title),
                "price_now": price_n, "price_was": price_w,
                "discount": disc, "summary": title,
                "image_url": img_url,
                "first_seen": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
            })
        except Exception as e:
            log.debug(f"  Newegg item error: {e}")

    log.info(f"  Newegg: {len(deals)} deals")
    return deals


def _scrape_newegg_html() -> list[dict]:
    """HTML fallback for Newegg Today's Deals."""
    log.info("  Newegg: trying HTML scrape…")
    deals = []
    try:
        resp = requests.get("https://www.newegg.com/todays-deals/pl",
                            headers=HEADERS, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(resp.text, "html.parser")

        for card in soup.select(".item-container")[:30]:
            try:
                title_el = card.select_one(".item-title")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                a_el  = title_el if title_el.name == "a" else title_el.find("a")
                url   = a_el.get("href", "") if a_el else ""
                if not url:
                    continue

                price_n_el = card.select_one(".price-current")
                price_w_el = card.select_one(".price-was, .price-old")
                price_n = _price(price_n_el.get_text()) if price_n_el else None
                price_w = _price(price_w_el.get_text()) if price_w_el else None
                disc    = _discount_pct(price_n, price_w)

                img_el  = card.select_one("img.item-img")
                img_url = img_el.get("src", "") if img_el else ""

                did = deal_id(title, url)
                deals.append({
                    "id": did, "title": title, "url": url,
                    "source": "Newegg", "category": "tech",
                    "score": _score(price_n, price_w, disc, title),
                    "price_now": price_n, "price_was": price_w,
                    "discount": disc, "summary": title,
                    "image_url": img_url,
                    "first_seen": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
                })
            except Exception:
                pass
    except Exception as e:
        log.warning(f"  Newegg HTML failed: {e}")

    log.info(f"  Newegg HTML: {len(deals)} deals")
    return deals


# ── 4. B&H PHOTO — Hot Deals ─────────────────────────────────────
def scrape_bh() -> list[dict]:
    """
    Fetch B&H Photo hot deals from their website.
    B&H renders product grids in HTML — bs4 works well here.
    """
    log.info("🛒 B&H Photo: fetching hot deals…")
    deals = []

    # Try their JSON search API first
    api_url = (
        "https://www.bhphotovideo.com/c/search"
        "?InitialCategoryID=25054&N=4294967055&sort=NEW&start=0&num=24"
        "&pf_rd_r=true&ajax=true"
    )
    try:
        resp = requests.get(api_url, headers={**HEADERS,
                            "Accept": "application/json"},
                            timeout=REQUEST_TIMEOUT)
        data = resp.json()
        items = data.get("products", [])
    except Exception:
        items = []

    if items:
        for item in items[:30]:
            try:
                title   = item.get("title", "").strip()
                if not title:
                    continue
                url     = "https://www.bhphotovideo.com" + item.get("url", "")
                price_n = _price(item.get("pricingInfo", {}).get("currentPrice"))
                price_w = _price(item.get("pricingInfo", {}).get("listPrice"))
                disc    = _discount_pct(price_n, price_w)
                img_url = item.get("imageUrl", "")

                did = deal_id(title, url)
                deals.append({
                    "id": did, "title": title, "url": url,
                    "source": "B&H Photo", "category": "tech",
                    "score": _score(price_n, price_w, disc, title),
                    "price_now": price_n, "price_was": price_w,
                    "discount": disc, "summary": title,
                    "image_url": img_url,
                    "first_seen": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
                })
            except Exception as e:
                log.debug(f"  B&H item error: {e}")
        log.info(f"  B&H Photo: {len(deals)} deals from API")
        return deals

    # HTML fallback
    log.info("  B&H: trying HTML scrape…")
    try:
        resp = requests.get(
            "https://www.bhphotovideo.com/c/buy/Hot-Deals/ci/25054/N/4294967055",
            headers=HEADERS, timeout=REQUEST_TIMEOUT)
        soup = BeautifulSoup(resp.text, "html.parser")

        for card in soup.select("[data-selenium='productThumbList'] > li, .product-wrap")[:30]:
            try:
                title_el = (card.select_one("[data-selenium='productTitle']") or
                            card.select_one(".title a") or
                            card.select_one("a.product-title"))
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)

                a_el = title_el if title_el.name == "a" else title_el.find_parent("a")
                href = (a_el.get("href", "") if a_el else "")
                if not href:
                    a_el = card.find("a", href=re.compile(r"/c/product/"))
                    href = a_el.get("href", "") if a_el else ""
                url = urljoin("https://www.bhphotovideo.com", href) if href else ""
                if not url:
                    continue

                price_n_el = card.select_one("[data-selenium='price'], .price")
                price_w_el = card.select_one(".price-was, .list-price, del")
                price_n = _price(price_n_el.get_text()) if price_n_el else None
                price_w = _price(price_w_el.get_text()) if price_w_el else None
                disc    = _discount_pct(price_n, price_w)

                img_el  = card.select_one("img")
                img_url = img_el.get("src", img_el.get("data-src", "")) if img_el else ""

                did = deal_id(title, url)
                deals.append({
                    "id": did, "title": title, "url": url,
                    "source": "B&H Photo", "category": "tech",
                    "score": _score(price_n, price_w, disc, title),
                    "price_now": price_n, "price_was": price_w,
                    "discount": disc, "summary": title,
                    "image_url": img_url,
                    "first_seen": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
                })
            except Exception:
                pass
    except Exception as e:
        log.warning(f"  B&H HTML failed: {e}")

    log.info(f"  B&H Photo HTML: {len(deals)} deals")
    return deals


# ── 5. DELL — Outlet + Promotions ────────────────────────────────
def scrape_dell() -> list[dict]:
    """
    Fetch Dell outlet/deals via their promotions API.
    Dell's outlet page serves product JSON that we can parse.
    """
    log.info("🛒 Dell: fetching outlet deals…")
    deals = []

    # Dell catalog API — outlet laptops
    endpoints = [
        ("https://www.dell.com/en-us/shop/category/outlet-laptops", "Laptops"),
        ("https://www.dell.com/en-us/shop/category/outlet-desktops", "Desktops"),
    ]

    for page_url, category in endpoints:
        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            soup = BeautifulSoup(resp.text, "html.parser")

            # Dell embeds product data in window.__PRELOADED_STATE__ JSON
            script = soup.find("script", string=re.compile(r"__PRELOADED_STATE__"))
            if script:
                m = re.search(r"__PRELOADED_STATE__\s*=\s*(\{.*?\});", script.string,
                              re.DOTALL)
                if m:
                    try:
                        state = json.loads(m.group(1))
                        products = (state.get("catalog", {})
                                       .get("products", []))
                        for p in products[:15]:
                            title   = p.get("title", "").strip()
                            if not title:
                                continue
                            url     = "https://www.dell.com" + p.get("url", "")
                            price_n = _price(p.get("pricing", {}).get("currentPrice"))
                            price_w = _price(p.get("pricing", {}).get("originalPrice"))
                            disc    = _discount_pct(price_n, price_w)
                            img_url = p.get("imageUrl", "")
                            did = deal_id(title, url)
                            deals.append({
                                "id": did, "title": title, "url": url,
                                "source": "Dell", "category": "tech",
                                "score": _score(price_n, price_w, disc, title),
                                "price_now": price_n, "price_was": price_w,
                                "discount": disc, "summary": f"Dell Outlet: {title}",
                                "image_url": img_url,
                                "first_seen": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
                            })
                        continue
                    except Exception:
                        pass

            # Fallback: HTML product cards
            for card in soup.select(".ps-product-card, .dell-card")[:15]:
                try:
                    title_el = card.select_one("h3, .product-title, [data-testid='product-title']")
                    if not title_el:
                        continue
                    title = title_el.get_text(strip=True)
                    a_el  = card.find("a", href=True)
                    href  = a_el.get("href", "") if a_el else ""
                    url   = urljoin("https://www.dell.com", href) if href else ""
                    if not url:
                        continue
                    price_n_el = card.select_one(".dell-price, .price-offer")
                    price_w_el = card.select_one(".price-strike, .crossed-price")
                    price_n = _price(price_n_el.get_text()) if price_n_el else None
                    price_w = _price(price_w_el.get_text()) if price_w_el else None
                    disc    = _discount_pct(price_n, price_w)
                    img_el  = card.find("img")
                    img_url = img_el.get("src", "") if img_el else ""
                    did = deal_id(title, url)
                    deals.append({
                        "id": did, "title": title, "url": url,
                        "source": "Dell", "category": "tech",
                        "score": _score(price_n, price_w, disc, title),
                        "price_now": price_n, "price_was": price_w,
                        "discount": disc, "summary": f"Dell Outlet: {title}",
                        "image_url": img_url,
                        "first_seen": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
                    })
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"  Dell scrape failed for {page_url}: {e}")

    log.info(f"  Dell: {len(deals)} deals")
    return deals


# ── 6. WALMART — Rollbacks & Clearance ───────────────────────────
def scrape_walmart() -> list[dict]:
    """
    Fetch Walmart rollback deals via their browse API.
    """
    log.info("🛒 Walmart: fetching rollback deals…")
    deals = []

    # Walmart's browse page search API
    api_url = (
        "https://www.walmart.com/search/api/ppsearch"
        "?query=rollback&sort=best_seller&page=1&affinityOverride=default"
        "&catId=3944&prg=desktop"
    )
    try:
        resp = requests.get(api_url, headers={**HEADERS,
                            "Accept": "application/json"},
                            timeout=REQUEST_TIMEOUT)
        data = resp.json()
        items = (data.get("items", []) or
                 data.get("products", []) or
                 data.get("payload", {}).get("products", []))
    except Exception as e:
        log.warning(f"  Walmart API failed: {e}")
        return []

    for item in items[:20]:
        try:
            title   = item.get("name", item.get("title", "")).strip()
            if not title:
                continue
            product_id = item.get("productId", item.get("id", ""))
            url     = (item.get("canonicalUrl", "") or
                       f"https://www.walmart.com/ip/{product_id}")
            if not url.startswith("http"):
                url = "https://www.walmart.com" + url
            price_n = _price(item.get("salePrice", item.get("price")))
            price_w = _price(item.get("wasPrice", item.get("priceInfo", {}).get("wasPrice")))
            disc    = _discount_pct(price_n, price_w)
            img_url = item.get("imageUrl", item.get("image", {}).get("url", ""))

            did = deal_id(title, url)
            deals.append({
                "id": did, "title": title, "url": url,
                "source": "Walmart", "category": "general",
                "score": _score(price_n, price_w, disc, title),
                "price_now": price_n, "price_was": price_w,
                "discount": disc, "summary": title,
                "image_url": img_url,
                "first_seen": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
            })
        except Exception as e:
            log.debug(f"  Walmart item error: {e}")

    log.info(f"  Walmart: {len(deals)} deals")
    return deals


# ─────────────────────────────────────────────────────────────────
#  IMAGE ENRICHMENT
#  For deals that have no image from the scraper, try fetching
#  the product page directly to extract og:image / Amazon hi-res.
# ─────────────────────────────────────────────────────────────────
def enrich_images(deals: list[dict]) -> list[dict]:
    """Fetch missing product images in parallel."""
    need_images = [d for d in deals if not d.get("image_url")]
    if not need_images:
        return deals

    log.info(f"🖼  Fetching images for {len(need_images)} deals…")

    def _fetch(deal):
        img = fetch_image(deal["url"])
        if img:
            deal["image_url"] = img
        return deal

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(_fetch, d): d for d in need_images}
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception:
                pass

    return deals


# ─────────────────────────────────────────────────────────────────
#  JSON EXPORT  — must match the exact format dealradar.py writes
#  so the website (index.html) can read both sources seamlessly.
#
#  Website field names:   DB column names:
#    store              ← source
#    merchant_url       ← url
#    price              ← price_now
#    original_price     ← price_was
#    discount_percent   ← discount
#    description        ← summary
#    upvotes            ← score
# ─────────────────────────────────────────────────────────────────
def export_json(output_dir: str = "."):
    """
    Read all recent deals from DB and write deals.json in the
    same format that dealradar.py produces (the format index.html expects).
    This runs after dealradar.py has already written deals.json, so it
    re-reads the full DB and regenerates the file with ALL deals merged.
    """
    cutoff = (datetime.datetime.utcnow() -
              datetime.timedelta(days=MAX_AGE_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        con = get_db()
        cur = con.cursor()
        rows = cur.execute("""
            SELECT id, title, url, source, category, score,
                   price_now, price_was, discount, summary,
                   first_seen, last_seen, image_url, status
            FROM   deals
            WHERE  last_seen >= ? AND status != 'gone'
            ORDER  BY score DESC, last_seen DESC
        """, (cutoff,)).fetchall()
        con.close()
    except Exception as e:
        log.error(f"export_json DB error: {e}")
        return

    _JUNK_EXPORT_HOSTS = {"play.google.com", "apps.apple.com"}

    deal_list = []
    for r in rows:
        (did, title, url, source, category, score,
         price_now, price_was, discount, summary,
         first_seen, last_seen, image_url, status) = r

        # Skip junk URLs
        if _host(url or "") in _JUNK_EXPORT_HOSTS:
            continue

        is_exp = (status == "expired")
        badge  = ("HOT" if (score or 0) >= 75 and not is_exp
                  else ("EXPIRED" if is_exp else ""))

        deal_list.append({
            "id":               did,
            "title":            title or "",
            "price":            price_now,
            "original_price":   price_was,
            "discount_percent": int(discount or 0),
            "store":            source or "",
            "badge":            badge,
            "category":         category or "",
            "merchant_url":     url or "",
            "image_url":        image_url or "",
            "description":      summary or "",
            "specs":            {},
            "upvotes":          score or 0,
            "downvotes":        0,
            "comment_count":    0,
            "posted_date":      (first_seen or "")[:10],
            "expires":          None,
            "expired":          is_exp,
            "status":           status or "active",
        })

    payload = {
        "version":   1,
        "generated": datetime.datetime.utcnow().strftime("%Y-%m-%d"),
        "deals":     deal_list,
    }
    out_path = os.path.join(output_dir, "deals.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    log.info(f"✅ deals.json written → {len(deal_list)} total deals ({out_path})")


# ─────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────
SCRAPERS = [
    ("Amazon",   scrape_amazon),
    ("Best Buy", scrape_bestbuy),
    ("Newegg",   scrape_newegg),
    ("B&H",      scrape_bh),
    ("Dell",     scrape_dell),
    ("Walmart",  scrape_walmart),
]


def run(top_n: int = 0, export: bool = True):
    log.info("=" * 60)
    log.info("DealPulses — Direct Merchant Scraper starting")
    log.info("=" * 60)

    ensure_schema()

    all_deals: list[dict] = []
    for name, scraper_fn in SCRAPERS:
        try:
            results = scraper_fn()
            all_deals.extend(results)
        except Exception as e:
            log.error(f"Scraper {name} crashed: {e}")

    log.info(f"📦 Total raw deals collected: {len(all_deals)}")

    # Enrich missing images
    all_deals = enrich_images(all_deals)

    # Filter: require at least a title and valid URL
    valid = [d for d in all_deals
             if d.get("title") and d.get("url", "").startswith("http")]
    log.info(f"✅ Valid deals after filtering: {len(valid)}")

    # Upsert into DB
    con = get_db()
    cur = con.cursor()
    for deal in valid:
        try:
            upsert_deal(cur, deal)
        except Exception as e:
            log.debug(f"upsert error for '{deal.get('title', '?')}': {e}")
    con.commit()
    con.close()
    log.info(f"💾 DB updated with {len(valid)} merchant deals")

    # Print top N if requested
    if top_n > 0:
        sorted_deals = sorted(valid, key=lambda d: d.get("score", 0), reverse=True)
        print(f"\n{'='*60}")
        print(f"  TOP {top_n} MERCHANT DEALS")
        print(f"{'='*60}")
        for i, d in enumerate(sorted_deals[:top_n], 1):
            price_str = f"${d['price_now']:.2f}" if d.get("price_now") else "N/A"
            disc_str  = f" ({d['discount']:.0f}% off)" if d.get("discount") else ""
            print(f"\n{i:2}. [{d['source']}] {d['title'][:60]}")
            print(f"    Price: {price_str}{disc_str} | Score: {d.get('score',0)}")
            print(f"    {d['url'][:80]}")

    # Export deals.json
    if export:
        export_json(EXPORT_DIR)

    log.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DealPulses Direct Merchant Scraper")
    parser.add_argument("--top", type=int, default=0,
                        help="Print top N deals to terminal (default: 0 = silent)")
    parser.add_argument("--no-export", action="store_true",
                        help="Skip writing deals.json")
    args = parser.parse_args()
    run(top_n=args.top, export=not args.no_export)
