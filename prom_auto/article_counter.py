import logging
import os
import threading

from . import prom_client

logger = logging.getLogger(__name__)

_COUNTER_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".article_counter")
_lock = threading.Lock()
# True once this process has reconciled the local file against Prom.ua's
# live catalog at least once. Checked/set inside _lock together with the
# reconciliation itself, so concurrent next_article() calls at process
# startup can't both decide reconciliation is still needed.
_reconciled = False


def next_article() -> str:
    """Returns a unique, human-readable 'vNNNN' article/SKU code.

    Used to be prom_client.count_products() + 1, but that queries Prom.ua's
    live catalog - and Prom.ua's import is async, so it doesn't reflect a
    just-submitted product right away. Two imports run seconds apart could
    both see the same count and generate the same article, and since
    Ідентифікатор_товару/Код_товару is Prom.ua's merge key, the second
    import silently overwrites the first instead of creating a second
    product (confirmed: rapid test imports today all collided into one
    product). A local, synchronous counter can't race with anything.

    The local file itself can still fall behind the live catalog between
    process runs - e.g. two bot instances ever running against the same
    Prom.ua account each keep their own local file, or the file gets
    reset/restored stale. When that happens, a "fresh" local article
    number can collide with one already used by an unrelated existing
    product, and Prom.ua's merge-on-import silently overwrites that
    unrelated product instead of creating a new one - confirmed in
    practice (local counter found 72 articles behind the live catalog,
    meaning every import in between was quietly overwriting older,
    unrelated products rather than creating new ones). Reconciling once
    against the live max at process startup (not the per-import network
    round trip find_max_article_number() was originally removed to avoid)
    catches that drift while still letting a whole session's rapid-fire
    imports run off the fast local counter afterward.
    """
    global _reconciled
    with _lock:
        current = _read_counter()
        if not _reconciled:
            _reconciled = True
            try:
                live_max = prom_client.find_max_article_number()
            except Exception:
                logger.exception("Could not reconcile article counter against Prom.ua's live catalog")
            else:
                if live_max > current:
                    logger.warning(
                        "Local article counter (%d) was behind Prom.ua's live catalog (%d) - "
                        "resyncing to avoid colliding with existing products",
                        current,
                        live_max,
                    )
                    current = live_max
        next_value = current + 1
        _write_counter(next_value)
    return f"v{next_value:04d}"


def external_id_for(article: str) -> str:
    """Prom.ua's Ідентифікатор_товару (external_id) value to pair with
    `article` (used for Код_товару/sku) - deliberately shaped differently
    from the plain "vNNNN" pattern, not just equal to it.

    Prom.ua's XLS import matches a row to an existing product by EITHER
    external_id or sku, whichever holds the given value - and this
    account's pre-existing catalog already has "vNNNN"-shaped sku values
    on real, unrelated products. Using plain "vNNNN" as external_id too
    meant a fresh article could coincidentally equal some old product's
    sku and silently merge into it - confirmed in practice: an import
    landed on live products, changing their price to the new item's price
    while leaving name/description untouched, instead of creating new
    products. This prefix can never appear in that legacy sku data, so it
    can't collide with it.
    """
    return f"PA-{article}"


def _read_counter() -> int:
    if os.path.exists(_COUNTER_FILE):
        with open(_COUNTER_FILE) as f:
            return int(f.read().strip())
    # First run ever: seed from the highest "vNNNN" article already used in
    # the account (not count_products() - the two can differ if Prom.ua's
    # async import hasn't fully reflected a recent product yet).
    return prom_client.find_max_article_number()


def _write_counter(value: int) -> None:
    with open(_COUNTER_FILE, "w") as f:
        f.write(str(value))
