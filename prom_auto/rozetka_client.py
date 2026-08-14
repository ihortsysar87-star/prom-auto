"""Rozetka Marketplace's own direct seller API (api-seller.rozetka.com.ua) -
a completely separate account/credentials from prom_client.py's Prom.ua API.
Spec pulled from https://api-seller.rozetka.com.ua/apidoc/ (its
api_data.json), since the rendered docs page is a JS SPA that doesn't
render outside a real browser.

Lets rozetka_backfill-style scripts fix vendor/characteristics directly on
Rozetka's own catalog, instead of going through a Prom.ua re-import and
hoping the auto-generated feed picks it up and Rozetka re-pulls it in time.
Whether a direct fix here actually survives Rozetka's next pull of the
Prom.ua-hosted feed (which is how this account's items got here in the
first place - "sync_source_id" in /goods/errors) is unconfirmed; test on a
single item before relying on this for the full backfill.
"""
from __future__ import annotations

import base64
import logging

import requests

from . import config

logger = logging.getLogger(__name__)

_BASE_URL = config.ROZETKA_SELLER_API_BASE_URL

_token_cache: dict[str, str] = {}


def _login() -> str:
    """Returns a bearer access_token: ROZETKA_SELLER_ACCESS_TOKEN as-is if
    set (a token generated directly in the seller panel), otherwise POST
    /sites to exchange ROZETKA_SELLER_USERNAME/PASSWORD for one. Login
    results are cached in-process - Rozetka extends a token's validity on
    each use (24h idle expiry per their docs), so re-logging in on every
    call isn't needed, only once this process's cache is empty."""
    if config.ROZETKA_SELLER_ACCESS_TOKEN:
        return config.ROZETKA_SELLER_ACCESS_TOKEN

    cached = _token_cache.get("access_token")
    if cached:
        return cached

    if not (config.ROZETKA_SELLER_USERNAME and config.ROZETKA_SELLER_PASSWORD):
        raise RuntimeError(
            "Set either ROZETKA_SELLER_ACCESS_TOKEN, or both "
            "ROZETKA_SELLER_USERNAME/ROZETKA_SELLER_PASSWORD, in .env"
        )

    password_b64 = base64.b64encode(config.ROZETKA_SELLER_PASSWORD.encode()).decode()
    response = requests.post(
        f"{_BASE_URL}/sites",
        json={"username": config.ROZETKA_SELLER_USERNAME, "password": password_b64},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"Rozetka login failed: {payload.get('errors')}")

    token = payload["content"]["access_token"]
    _token_cache["access_token"] = token
    return token


def _headers(version: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {_login()}", "Content-Language": "uk"}
    if version:
        headers["Version"] = version
    return headers


def _get(path: str, *, params: dict | None = None, version: str | None = None) -> dict:
    response = requests.get(f"{_BASE_URL}{path}", headers=_headers(version), params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"Rozetka API error on GET {path}: {payload.get('errors')}")
    return payload["content"]


def get_items_with_errors(**params) -> dict:
    """GET /goods/errors - products Rozetka's own catalog currently flags
    with problems. Same data seller.rozetka.com.ua's validator page shows,
    as clean JSON instead of scraped HTML - e.g. category_id, producer_id,
    price_offer_id filters are all supported, see apidoc "ApiItems" group."""
    return _get("/goods/errors", params=params)


def get_category_options(category_id: int) -> list[dict]:
    """GET /v1/market-categories/category-options - the exact
    characteristics (id/name/attr_type/unit/allowed values) Rozetka expects
    for a given category, instead of guessing at a column format."""
    return _get(
        "/v1/market-categories/category-options", params={"category_id": category_id}, version="0.0.1"
    )


def get_update_template(category_id: int) -> bytes:
    """GET /items-file-import/template - the category-correct xlsx column
    layout to build an update file against (includes the item-id column
    Rozetka needs to match rows to existing listings)."""
    response = requests.get(
        f"{_BASE_URL}/items-file-import/template",
        headers=_headers(),
        params={"category_id": category_id},
        timeout=30,
    )
    response.raise_for_status()
    return response.content


def update_items(xlsx_bytes: bytes) -> int:
    """POST /items-file-import/update-items - updates existing Rozetka
    items in bulk from an xlsx file (must include the item's own ID column
    from the template, or Rozetka can't match it to an existing listing).
    Returns the import job id - poll get_import_status() with it."""
    response = requests.post(
        f"{_BASE_URL}/items-file-import/update-items",
        headers=_headers(),
        files={"file": ("update.xlsx", xlsx_bytes)},
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"Rozetka update-items failed: {payload.get('errors')}")
    return payload["content"]["id"]


def get_import_status(import_id: int) -> dict:
    """GET /items-file-import/import-status - outcome of an update_items()
    (or create-items) job: parsing/validity counts, and error/warning
    counts once Rozetka has finished processing the file."""
    return _get("/items-file-import/import-status", params={"id": import_id})
