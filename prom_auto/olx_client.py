from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import requests

from . import config

logger = logging.getLogger(__name__)

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

_COMMON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Version": "2.0",
}

# In-memory cache for the current access token, refreshed from
# config.OLX_REFRESH_TOKEN on demand. Not persisted - unlike the refresh
# token, losing it on restart just costs one extra refresh call.
_access_token: str | None = None
_access_token_expires_at: float = 0.0
# Refresh a bit before the token's actual expiry to avoid a request landing
# right on the boundary and getting a 403 mid-flight.
_EXPIRY_SAFETY_MARGIN_SECONDS = 60


class OlxAuthError(Exception):
    """OLX's OAuth token endpoint rejected a request - most commonly a dead
    refresh_token (unused for 30 days, or already superseded by a newer one)
    that requires re-running olx_auth_setup.py to get a fresh one."""


def _persist_refresh_token(new_refresh_token: str) -> None:
    """OLX rotates the refresh_token on (roughly) every use - the docs warn
    the old one may stop working once a new one is issued, so the new value
    has to be written back to .env (not just kept in memory) or the next
    process restart would authenticate with a stale token and fail."""
    config.OLX_REFRESH_TOKEN = new_refresh_token
    if not _ENV_PATH.exists():
        logger.warning(".env not found at %s - new OLX refresh_token not persisted to disk", _ENV_PATH)
        return

    text = _ENV_PATH.read_text()
    new_line = f"OLX_REFRESH_TOKEN={new_refresh_token}"
    if re.search(r"^OLX_REFRESH_TOKEN=.*$", text, flags=re.MULTILINE):
        text = re.sub(r"^OLX_REFRESH_TOKEN=.*$", new_line, text, count=1, flags=re.MULTILINE)
    else:
        text = text.rstrip("\n") + f"\n{new_line}\n"
    _ENV_PATH.write_text(text)


def _request_token(payload: dict) -> dict:
    response = requests.post(
        config.OLX_TOKEN_URL,
        json={**payload, "client_id": config.OLX_CLIENT_ID, "client_secret": config.OLX_CLIENT_SECRET},
        headers=_COMMON_HEADERS,
        timeout=15,
    )
    if not response.ok:
        raise OlxAuthError(f"OLX token request failed ({response.status_code}): {response.text}")
    return response.json()


def exchange_code_for_tokens(code: str, redirect_uri: str) -> dict:
    """Grant type authorization_code - the one-time exchange olx_auth_setup.py
    performs after the user logs in and approves access. Returns the raw
    token response ({access_token, refresh_token, expires_in, ...})."""
    return _request_token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "scope": "v2 read write",
            "redirect_uri": redirect_uri,
        }
    )


def _refresh_access_token() -> str:
    global _access_token, _access_token_expires_at

    if not config.OLX_REFRESH_TOKEN:
        raise OlxAuthError(
            "No OLX_REFRESH_TOKEN configured - run `python -m prom_auto.olx_auth_setup` once to log in."
        )

    tokens = _request_token(
        {"grant_type": "refresh_token", "refresh_token": config.OLX_REFRESH_TOKEN}
    )
    _access_token = tokens["access_token"]
    _access_token_expires_at = time.monotonic() + tokens.get("expires_in", 0)
    new_refresh_token = tokens.get("refresh_token")
    if new_refresh_token and new_refresh_token != config.OLX_REFRESH_TOKEN:
        _persist_refresh_token(new_refresh_token)
    return _access_token


def get_access_token() -> str:
    """Returns a currently-valid access token, refreshing it via the stored
    refresh_token if this is the first call or the cached one is about to
    expire. Callers never need to think about expiry themselves."""
    if _access_token is None or time.monotonic() >= _access_token_expires_at - _EXPIRY_SAFETY_MARGIN_SECONDS:
        return _refresh_access_token()
    return _access_token


def _auth_headers() -> dict:
    return {**_COMMON_HEADERS, "Authorization": f"Bearer {get_access_token()}"}


_MAX_RATE_LIMIT_RETRIES = 5


def _request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    """Wraps requests.<method> with retry-on-429 - OLX's "Too many requests"
    response - since olx_sync makes several calls per product across
    potentially hundreds of products in one run, well within reach of any
    partner-API rate limit. Honors Retry-After when OLX sends one, otherwise
    backs off with a fixed delay."""
    for attempt in range(1, _MAX_RATE_LIMIT_RETRIES + 1):
        response = requests.request(method, url, **kwargs)
        if response.status_code != 429 or attempt == _MAX_RATE_LIMIT_RETRIES:
            return response
        wait_seconds = int(response.headers.get("Retry-After", 10))
        logger.warning(
            "OLX rate limit hit on %s %s, waiting %ds (attempt %d/%d)",
            method,
            url,
            wait_seconds,
            attempt,
            _MAX_RATE_LIMIT_RETRIES,
        )
        time.sleep(wait_seconds)
    return response


def _get(path: str, params: dict | None = None) -> dict:
    response = _request_with_retry(
        "GET", f"{config.OLX_API_BASE_URL}{path}", headers=_auth_headers(), params=params, timeout=15
    )
    response.raise_for_status()
    return response.json()


def suggest_categories(title: str) -> list[dict]:
    """GET /categories/suggestion?q=<title> - best-guess category matches for
    an ad title, ordered by relevance. Each entry has {id, name, path}; not
    all of them are necessarily leaf categories (only leaves are valid for
    category_id on advert creation - see get_category)."""
    data = _get("/categories/suggestion", params={"q": title})
    return data.get("data", data) if isinstance(data, dict) else data


def get_category(category_id: int) -> dict:
    """GET /categories/{id} - includes is_leaf and photos_limit, needed to
    validate a suggested category before using it on an advert."""
    data = _get(f"/categories/{category_id}")
    return data.get("data", data) if isinstance(data, dict) else data


def get_category_attributes(category_id: int) -> list[dict]:
    """GET /categories/{id}/attributes - the attribute definitions (code,
    label, validation, allowed values) OLX expects/accepts for this specific
    leaf category. Required-ness is inside each entry's `validation`."""
    data = _get(f"/categories/{category_id}/attributes")
    return data.get("data", data) if isinstance(data, dict) else data


def create_advert(payload: dict) -> dict:
    """POST /adverts - creates the advert under the currently authenticated
    (refresh-token-owning) user's account. Returns the full advert model on
    success, including its id/url/status. Raises requests.HTTPError with the
    response body attached via .response on validation failure (400) - the
    body's `error.validation` array names exactly which field(s) were
    rejected, e.g. a missing required category attribute."""
    response = _request_with_retry(
        "POST",
        f"{config.OLX_API_BASE_URL}/adverts",
        headers=_auth_headers(),
        json=payload,
        timeout=30,
    )
    if not response.ok:
        logger.error("OLX create_advert failed (%d): %s", response.status_code, response.text)
    response.raise_for_status()
    return response.json()


def get_advert(advert_id: int) -> dict:
    """GET /adverts/{id} - the full current advert model."""
    data = _get(f"/adverts/{advert_id}")
    return data.get("data", data) if isinstance(data, dict) else data


def update_advert(advert_id: int, payload: dict) -> dict:
    """PUT /adverts/{id} - like create_advert, this replaces the whole
    advert body (title/description/images/etc. all required again, not a
    partial patch)."""
    response = _request_with_retry(
        "PUT",
        f"{config.OLX_API_BASE_URL}/adverts/{advert_id}",
        headers=_auth_headers(),
        json=payload,
        timeout=30,
    )
    if not response.ok:
        logger.error("OLX update_advert(%s) failed (%d): %s", advert_id, response.status_code, response.text)
    response.raise_for_status()
    return response.json()
