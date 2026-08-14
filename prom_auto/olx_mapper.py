from __future__ import annotations

import datetime
import logging
import re

from . import config, olx_client

logger = logging.getLogger(__name__)

_MIN_TITLE_LEN = 16
_MAX_TITLE_LEN = 150
_MIN_DESCRIPTION_LEN = 80
_MAX_DESCRIPTION_LEN = 9000

_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
# Prom.ua's own images.prom.ua URLs embed a deliberate downscale target,
# e.g. ".../7603848361_w200_h200_slug.jpg" - confirmed by direct comparison
# that stripping it (".../7603848361_slug.jpg") serves the same image at its
# actual stored resolution instead of a shrunk-down thumbnail.
_IMAGE_SIZE_SUFFIX_PATTERN = re.compile(r"_w\d+_h\d+_")
# Prom.ua's AI-written descriptions/titles are sprinkled with emoji (✨, 📏,
# etc.) - confirmed by reproducing it directly that OLX's title/description
# validation rejects these outright ("Поле містить недопустимі символи
# та/або цифри"), failing advert creation for every product whose text
# happens to include one.
_EMOJI_PATTERN = re.compile(
    "[\U0001F000-\U0001FFFF\U00002190-\U000023FF\U00002600-\U000027BF\U00002B00-\U00002BFF]+"
)


class OlxMappingError(Exception):
    """A Prom.ua product couldn't be turned into a valid OLX advert payload
    at all (as opposed to OLX itself rejecting a well-formed one) - the
    caller should skip this product and move on rather than abort the sync."""


class CategoryNotFoundError(OlxMappingError):
    """None of OLX's suggested categories for this product's title resolved
    to a leaf category (the only kind category_id accepts) - posting can't
    proceed without a human picking one manually."""


def _plain_text(html: str) -> str:
    """OLX's description field is plain text for every category except Jobs
    (unlike Prom.ua's, which is HTML) - <br/> tags used for Prom's line
    breaks would otherwise show up as literal text."""
    text = _HTML_TAG_PATTERN.sub("\n", html)
    text = _EMOJI_PATTERN.sub("", text)
    return text.strip()


def _full_resolution(image_url: str) -> str:
    return _IMAGE_SIZE_SUFFIX_PATTERN.sub("_", image_url)


def _resolve_category(title: str) -> dict:
    suggestions = olx_client.suggest_categories(title)
    for suggestion in suggestions[:5]:
        category = olx_client.get_category(suggestion["id"])
        if category.get("is_leaf"):
            return category
    raise CategoryNotFoundError(
        f"No leaf category found among OLX's suggestions for {title!r}: {suggestions}"
    )


def _condition_attribute(category_id: int) -> dict | None:
    """Looks up the category's `state` attribute (used/new) and returns it
    filled in with config.OLX_CONDITION, or None if this category doesn't
    have one. Other required attributes (e.g. a phone's manufacturer) vary
    too much per category to guess reliably here - if any are missing, OLX's
    own validation error names exactly which one, surfaced the same way a
    Prom.ua import error already is."""
    for attribute in olx_client.get_category_attributes(category_id):
        if attribute.get("code") != "state":
            continue
        allowed = {value["code"] for value in attribute.get("values", [])}
        if config.OLX_CONDITION in allowed:
            return {"code": "state", "value": config.OLX_CONDITION}
        logger.warning(
            "Category %s's state attribute doesn't accept %r (allowed: %s)",
            category_id,
            config.OLX_CONDITION,
            allowed,
        )
    return None


def _effective_price(product: dict) -> float:
    """Prom.ua's `price` field is the undiscounted base price - the active
    discount (set via prom_client.edit_products, e.g. link-mode's automatic
    5%) is applied only at Prom.ua's own display layer, never folded back
    into `price`. OLX's /adverts has no discount-schedule concept of its
    own, so the actually-advertised price has to be computed here instead,
    only for a discount whose date range covers today."""
    price = product.get("price") or 0
    discount = product.get("discount")
    if not discount or discount.get("type") != "percent":
        return price
    try:
        start = datetime.datetime.strptime(discount["date_start"], "%d.%m.%Y").date()
        end = datetime.datetime.strptime(discount["date_end"], "%d.%m.%Y").date()
    except (KeyError, ValueError):
        return price
    if not (start <= datetime.date.today() <= end):
        return price
    return round(price * (1 - discount.get("value", 0) / 100), 2)


def build_olx_advert_from_prom_product(product: dict) -> dict:
    """Maps an existing Prom.ua product (as returned by
    prom_client.list_products) into OLX's POST /adverts payload, for
    olx_sync's catalog migration.

    Unlike Prom.ua's generic xlsx import, OLX requires a specific leaf
    category_id per advert - resolved here via OLX's own
    /categories/suggestion endpoint - plus a fixed city/contact/advertiser
    type shared by every listing (this account only lists as one seller).
    Prom's own description already has brand/model/etc. folded in as HTML
    "- Бренд: X;" lines, so it's reused as-is (stripped to plain text)
    rather than reconstructed from separate fields.
    """
    title = (product.get("name_multilang") or {}).get("uk") or product.get("name", "")
    title = _EMOJI_PATTERN.sub("", title).strip()[:_MAX_TITLE_LEN]
    if len(title) < _MIN_TITLE_LEN:
        raise OlxMappingError(f"Product name too short for an OLX title: {title!r}")

    description_html = (product.get("description_multilang") or {}).get("uk") or product.get("description", "")
    description = _plain_text(description_html)
    if len(description) < _MIN_DESCRIPTION_LEN:
        description = description.ljust(_MIN_DESCRIPTION_LEN)
    description = description[:_MAX_DESCRIPTION_LEN]

    category = _resolve_category(title)
    category_id = int(category["id"])
    attributes = []
    condition = _condition_attribute(category_id)
    if condition:
        attributes.append(condition)

    images = [
        {"url": _full_resolution(image["url"])} for image in product.get("images") or [] if image.get("url")
    ]
    # 0 means "no cap" for this category (seen on categories with no photo
    # limit at all) - only truncate when it's a genuine positive limit,
    # otherwise OLX rejects the whole advert with "Image error: Перевищено
    # ліміт кількості зображень" instead of just dropping the extras itself.
    photos_limit = category.get("photos_limit") or 0
    if photos_limit > 0:
        images = images[:photos_limit]

    return {
        "title": title,
        "description": description,
        "category_id": category_id,
        "advertiser_type": config.OLX_ADVERTISER_TYPE,
        "external_id": product.get("external_id") or product.get("sku") or str(product["id"]),
        "contact": {
            "name": config.OLX_CONTACT_NAME,
            "phone": config.OLX_CONTACT_PHONE,
        },
        "location": {"city_id": int(config.OLX_CITY_ID), "district_id": int(config.OLX_DISTRICT_ID)},
        "images": images,
        "price": {
            "value": _effective_price(product),
            "currency": product.get("currency") or "UAH",
            "negotiable": False,
            "trade": False,
            "budget": False,
        },
        "attributes": attributes,
        "auto_extend_enabled": True,
    }
