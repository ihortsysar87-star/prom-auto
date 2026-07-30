from __future__ import annotations

import json
import logging
import time

import requests
from bs4 import BeautifulSoup

from . import config, image_host, openai_client, page_fetch, product_image_scraper

logger = logging.getLogger(__name__)

PAGE_TEXT_MAX_CHARS = 6000

# Every link-mode product is auto-priced 5% below its confirmed source
# price, so the shop is reliably cheaper than the page it was sourced from.
DISCOUNT_RATE = 0.05

_FX_CACHE_TTL_SECONDS = 3600
_fx_cache: dict[str, tuple[float, float]] = {}  # currency -> (rate_to_uah, fetched_at)


class ProductNotFoundError(Exception):
    """The page couldn't be identified as a real product (error page, empty,
    CAPTCHA wall, or link doesn't point at a product at all)."""


def _fetch_fx_rate_to_uah(currency: str) -> float:
    """1 unit of `currency` in UAH, via a free no-key FX API. Cached for an
    hour so a batch of same-currency products (e.g. several EUR listings)
    doesn't refetch the rate per item."""
    currency = currency.upper()
    if currency == "UAH":
        return 1.0

    cached = _fx_cache.get(currency)
    now = time.time()
    if cached and now - cached[1] < _FX_CACHE_TTL_SECONDS:
        return cached[0]

    response = requests.get(f"https://open.er-api.com/v6/latest/{currency}", timeout=15)
    response.raise_for_status()
    payload = response.json()
    if payload.get("result") != "success":
        raise ValueError(f"FX lookup failed for {currency}: {payload}")
    rate = float(payload["rates"]["UAH"])
    _fx_cache[currency] = (rate, now)
    return rate


def convert_to_uah(amount: float, currency: str) -> float:
    return amount * _fetch_fx_rate_to_uah(currency)


def _extract_json_ld_product(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        for entry in payload if isinstance(payload, list) else [payload]:
            if isinstance(entry, dict) and entry.get("@type") == "Product":
                return entry
    return None


def _visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return " ".join(soup.stripped_strings)[:PAGE_TEXT_MAX_CHARS]


def _json_ld_price(json_ld: dict) -> tuple[float, str] | None:
    offers = json_ld.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        return None
    price, currency = offers.get("price"), offers.get("priceCurrency")
    if price is None or not currency:
        return None
    try:
        return float(price), str(currency)
    except (TypeError, ValueError):
        return None


def _json_ld_images(json_ld: dict) -> list[str]:
    image = json_ld.get("image")
    if isinstance(image, str):
        return [image] if image else []
    if isinstance(image, list):
        return [u for u in image if isinstance(u, str) and u]
    return []


def _gather_image_urls(url: str, json_ld: dict | None) -> list[str]:
    try:
        urls = product_image_scraper.find_product_image_urls(url)
    except Exception:
        logger.info("product_image_scraper failed for %s, falling back to JSON-LD images", url)
        urls = []
    if not urls and json_ld:
        urls = _json_ld_images(json_ld)
    return urls[: product_image_scraper.MAX_IMAGES]


def _host_images(urls: list[str]) -> list[str]:
    """Downloads, quality-checks, and (when imgbb is configured) re-hosts
    scraped marketplace photos - the same treatment photo-mode gives its
    scraped marketplace photos. Without IMGBB_API_KEY, or if hosting fails,
    falls back to linking the source URLs directly."""
    # Belt-and-suspenders against a blank entry reaching Telegram later (a
    # blank "image" in a page's JSON-LD reached here once and crashed
    # send_media_group with "Invalid file http url specified: url host is
    # empty" - the two JSON-LD extractors are now fixed at the source too,
    # but this is the last mile before these URLs go to Telegram).
    urls = [u for u in urls if u and u.strip()]

    if not config.IMGBB_API_KEY:
        return urls

    hosted = []
    for url in urls:
        try:
            img_bytes = product_image_scraper.fetch_image_bytes(url)
        except Exception:
            logger.warning("Failed to download scraped image %s, skipping", url)
            continue
        if not product_image_scraper.is_acceptable_quality(img_bytes):
            continue
        try:
            hosted.append(image_host.upload_image(img_bytes))
        except Exception:
            logger.exception("Failed to re-host %s, linking source URL instead", url)
            hosted.append(url)
    return hosted or urls


def identify_product_from_url(url: str) -> tuple[dict, list[str]]:
    """Link-mode pipeline: fetch the page, extract structured (JSON-LD) data
    when present, ask OpenAI to translate/localize/generate keywords from
    it, resolve the source price in UAH (code-driven currency conversion,
    never model arithmetic), and gather product images. Returns (data,
    image_urls), where `data` matches the schema product_mapper.
    build_prom_product() expects.

    priceUAH is the real, undiscounted source price - DISCOUNT_RATE is
    *not* baked into it. Prom.ua has its own native price/discount
    mechanism (POST /products/edit's `discount` field), which computes and
    displays the reduced price itself; pre-multiplying it away here would
    just hide the real source price from Prom.ua and from anyone auditing
    the number. See telegram_bot._apply_link_mode_discounts, which applies
    DISCOUNT_RATE through that API after the product is created.

    Raises ProductNotFoundError if the page isn't recognizable as a real
    product.
    """
    html, is_html = page_fetch.fetch_html(url)
    json_ld = _extract_json_ld_product(html) if is_html else None
    page_text = _visible_text(html) if is_html else html[:PAGE_TEXT_MAX_CHARS]

    enriched = openai_client.extract_product_from_page(url, json_ld, page_text)
    if enriched.get("error"):
        raise ProductNotFoundError(enriched["error"])

    price_info = _json_ld_price(json_ld) if json_ld else None
    if price_info is None and enriched.get("price") is not None and enriched.get("price_currency"):
        price_info = (float(enriched["price"]), str(enriched["price_currency"]))

    price_found = price_info is not None
    if price_found:
        amount, currency = price_info
        source_price_uah = round(convert_to_uah(amount, currency), 2)
    else:
        source_price_uah = 0

    data = {
        "name": enriched.get("name"),
        "name_ru": enriched.get("name_ru"),
        "model": enriched.get("model"),
        "brand": enriched.get("brand"),
        "manufacturer": enriched.get("manufacturer"),
        "country": enriched.get("country"),
        "material": enriched.get("material"),
        "material_ru": enriched.get("material_ru"),
        "color": enriched.get("color"),
        "color_ru": enriched.get("color_ru"),
        "width": enriched.get("width"),
        "height": enriched.get("height"),
        "length": enriched.get("length"),
        "weight": enriched.get("weight"),
        "description": enriched.get("description"),
        "description_ru": enriched.get("description_ru"),
        "priceUAH": source_price_uah,
        "price_found": price_found,
        "keywords": enriched.get("keywords") or [],
        "keywords_ru": enriched.get("keywords_ru") or [],
    }

    image_urls = _host_images(_gather_image_urls(url, json_ld))

    return data, image_urls
