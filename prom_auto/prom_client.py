import logging
import re
import time

import requests

from . import config

logger = logging.getLogger(__name__)

_HEADERS = {"Authorization": f"Bearer {config.PROM_API_TOKEN}"}

_MAX_TRIES = 5
_RETRY_WAIT_SECONDS = 5
_CONCURRENT_IMPORT_MARKER = "одновременных импорт"

# POST /products/import_file's own "status" field only reflects whether the
# *file* passed input validation - the actual per-row outcome (created vs.
# silently rejected for a bad price/category/etc.) is only known once
# Prom.ua's async job finishes, checked via a completely different endpoint
# (GET /products/import/status/{id}). These bound how long import_and_wait
# polls that endpoint before giving up and returning whatever the last
# response was.
_IMPORT_STATUS_MAX_POLLS = 15
_IMPORT_STATUS_POLL_SECONDS = 4
# Terminal values of ImportStatus.status (compared case-insensitively - the
# public API docs give them upper-case, but casing elsewhere in this API is
# inconsistent). Anything else means the job is still processing.
_IMPORT_TERMINAL_STATUSES = {"success", "partial", "fatal"}


class PromImportBusyError(Exception):
    """Prom.ua is refusing new imports - either a normal transient lock, or
    their known nightly restriction, which can outlast a short retry."""


class PromImportStillProcessingError(Exception):
    """Prom.ua's async import job hadn't reached a terminal status by the
    time we stopped polling - not necessarily a failure, just unresolved.
    Carries the last status payload we did get, if any."""

    def __init__(self, message: str, last_status: dict | None):
        super().__init__(message)
        self.last_status = last_status


def count_products() -> int:
    """Total number of products currently in the Prom.ua account.

    Prom.ua's API has no dedicated count endpoint, so this pages through
    GET /products/list (sorted by id) and sums how many come back, using
    each page's lowest id as the next page's upper bound.
    """
    total = 0
    last_id = None
    page_size = 100
    while True:
        params = {"limit": page_size}
        if last_id is not None:
            params["last_id"] = last_id
        response = requests.get(
            f"{config.PROM_API_BASE_URL}/products/list",
            headers=_HEADERS,
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        products = response.json().get("products", [])
        if not products:
            break
        total += len(products)
        if len(products) < page_size:
            break
        last_id = min(p["id"] for p in products) - 1
    return total


_ARTICLE_PATTERN = re.compile(r"^v(\d+)$", re.IGNORECASE)


def find_max_article_number() -> int:
    """Scans existing products for 'vNNNN'-style external_id/sku and returns
    the highest N found. Used to seed article_counter's local counter, since
    count_products() alone isn't safe here: Prom.ua's import is async, so a
    just-submitted product's article might already be higher than the
    current live count reflects.
    """
    max_number = 0
    last_id = None
    page_size = 100
    while True:
        params = {"limit": page_size}
        if last_id is not None:
            params["last_id"] = last_id
        response = requests.get(
            f"{config.PROM_API_BASE_URL}/products/list",
            headers=_HEADERS,
            params=params,
            timeout=30,
        )
        response.raise_for_status()
        products = response.json().get("products", [])
        if not products:
            break
        for product in products:
            for value in (product.get("external_id"), product.get("sku")):
                match = _ARTICLE_PATTERN.match(str(value or ""))
                if match:
                    max_number = max(max_number, int(match.group(1)))
        if len(products) < page_size:
            break
        last_id = min(p["id"] for p in products) - 1
    return max_number


def import_file(xlsx_bytes: bytes) -> dict:
    """Equivalent of the n8n 'HTTP Request to prom' node
    (POST /products/import_file, multipart xlsx upload). Retries on
    Prom.ua's "another import is already running" error, matching the
    original n8n node's 5 tries / 5s wait."""
    for attempt in range(1, _MAX_TRIES + 1):
        response = requests.post(
            f"{config.PROM_API_BASE_URL}/products/import_file",
            headers=_HEADERS,
            files={"file": ("products.xlsx", xlsx_bytes)},
        )
        if response.ok:
            return response.json()

        try:
            error_message = response.json().get("error", {}).get("message", "")
        except ValueError:
            error_message = response.text

        if _CONCURRENT_IMPORT_MARKER in error_message:
            if attempt < _MAX_TRIES:
                logger.warning(
                    "Prom.ua busy with a previous import, retrying (%d/%d)", attempt, _MAX_TRIES
                )
                time.sleep(_RETRY_WAIT_SECONDS)
                continue
            raise PromImportBusyError(
                "Prom.ua is still refusing imports after retrying - likely their "
                "known nightly restriction rather than a quick transient lock."
            )

        logger.error("Prom.ua import_file error response: %s", error_message)
        response.raise_for_status()


def get_import_status(import_id: str) -> dict:
    """GET /products/import/status/{id} - the actual outcome of an async
    import job: counts of created/updated/with_errors_count positions and,
    when any row failed, an `errors` array describing why."""
    response = requests.get(
        f"{config.PROM_API_BASE_URL}/products/import/status/{import_id}",
        headers=_HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def get_product_by_external_id(external_id: str) -> dict | None:
    """GET /products/by_external_id/{id} - looks a product up by the
    external_id/sku we gave it (our "vNNNN" article), returning Prom.ua's
    own numeric product id and its current price/discount/etc. Returns None
    if Prom.ua has no product under that external_id (e.g. the xlsx import
    never actually created/updated it, despite an apparently successful
    upload)."""
    response = requests.get(
        f"{config.PROM_API_BASE_URL}/products/by_external_id/{external_id}",
        headers=_HEADERS,
        timeout=15,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json().get("product")


def edit_products(edits: list[dict]) -> dict:
    """POST /products/edit - batched edits keyed by Prom.ua's own numeric
    product id (not external_id/sku). Used to set a native price/discount
    (the `discount` field: {value, type: "percent"|"amount"}) so Prom.ua
    computes and displays the reduced price itself, instead of the bot
    pre-multiplying the price and hiding the real source price. Returns
    {"processed_ids": [...], "errors": {...}}."""
    response = requests.post(
        f"{config.PROM_API_BASE_URL}/products/edit",
        headers=_HEADERS,
        json=edits,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _import_status_is_settled(status_payload: dict) -> bool:
    """Whether an ImportStatus response actually reflects a finished job,
    not just a terminal-looking `status` value. Confirmed in practice:
    Prom.ua can report status="SUCCESS" on the very first poll while every
    count (created/updated/not_changed/actualized/with_errors_count) is
    still 0 for a file with a non-zero `total` - i.e. the file was accepted
    but nothing had actually been processed yet. Trusting `status` alone
    there made the bot declare success for an import that (as far as we
    could tell) did nothing. If total rows were submitted, treat it as
    settled only once *some* count shows the job actually did something."""
    status = str(status_payload.get("status", "")).lower()
    if status not in _IMPORT_TERMINAL_STATUSES:
        return False
    total = status_payload.get("total") or 0
    if total == 0:
        return True
    progress = sum(
        status_payload.get(key) or 0
        for key in ("created", "updated", "not_changed", "actualized", "imported", "with_errors_count")
    )
    return progress > 0


def import_and_wait(xlsx_bytes: bytes) -> dict:
    """Uploads the file via import_file(), then polls get_import_status()
    until Prom.ua's async job actually finishes. import_file()'s own
    response only confirms the file passed input validation - a file can
    clear that stage and still have every row silently rejected during
    processing (bad price, missing category, etc.), with no other signal
    unless something calls this status endpoint. Returns the final
    ImportStatus payload (with the job id added back in as "import_id",
    since Prom.ua's status response itself doesn't echo it).

    Raises PromImportStillProcessingError if the job hasn't reached a
    genuinely settled status within the poll budget - the import may still
    complete later, this just means we stopped waiting for it.
    """
    posted = import_file(xlsx_bytes)
    import_id = posted.get("id")
    if not import_id:
        raise PromImportStillProcessingError(
            "Prom.ua's import_file response had no job id to check status for", posted
        )

    last_status: dict | None = None
    for attempt in range(1, _IMPORT_STATUS_MAX_POLLS + 1):
        time.sleep(_IMPORT_STATUS_POLL_SECONDS)
        last_status = get_import_status(import_id)
        if _import_status_is_settled(last_status):
            last_status["import_id"] = import_id
            return last_status
        logger.info(
            "Prom.ua import %s not settled yet (poll %d/%d): %s",
            import_id,
            attempt,
            _IMPORT_STATUS_MAX_POLLS,
            last_status,
        )

    if last_status is not None:
        last_status["import_id"] = import_id
    raise PromImportStillProcessingError(
        f"Prom.ua import {import_id} did not settle within the poll budget", last_status
    )
