"""Pushes existing Prom.ua catalog products to OLX as adverts.

Run once to migrate the current catalog, or re-run any time afterward - it
tracks what's already been synced in .olx_synced.json (keyed by Prom's
external_id) and only creates adverts for products not in there yet, so
re-running is safe and cheap.

Never spends money: if OLX reports an advert "limited" (the free listing
quota for its category is used up), it's left inactive as-is - no packet is
purchased and no activate command is sent.

Usage:
    python -m prom_auto.olx_sync [--limit N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from . import olx_client, olx_mapper, prom_client

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

_STATE_PATH = Path(__file__).resolve().parent.parent / ".olx_synced.json"


def _load_state() -> dict:
    if _STATE_PATH.exists():
        return json.loads(_STATE_PATH.read_text())
    return {}


def _record_synced(external_id: str, entry: dict) -> None:
    """Re-reads the on-disk state and merges this one entry in before
    writing, rather than dumping the whole in-memory `state` dict - this
    script is meant to be re-run/left running for a long time, and blindly
    overwriting with a stale in-memory snapshot has actually clobbered
    entries added by another concurrently-running process before."""
    on_disk = _load_state()
    on_disk[external_id] = entry
    _STATE_PATH.write_text(json.dumps(on_disk, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=None, help="Stop after this many NEW products (for testing)"
    )
    parser.add_argument(
        "--from-article",
        default=None,
        help="Only sync products whose article ('vNNNN') is >= this, e.g. v0554",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Build and print advert payloads without calling OLX"
    )
    args = parser.parse_args()

    threshold = None
    if args.from_article:
        match = prom_client._ARTICLE_PATTERN.match(args.from_article)
        if not match:
            parser.error(f"--from-article must look like 'v0554', got {args.from_article!r}")
        threshold = int(match.group(1))

    state = _load_state()
    created = limited = skipped = failed = processed = 0

    for product in prom_client.list_products():
        external_id = product.get("external_id") or product.get("sku") or str(product["id"])
        name = product.get("name", "")

        if threshold is not None:
            article_match = prom_client._ARTICLE_PATTERN.match(str(external_id))
            if not article_match or int(article_match.group(1)) < threshold:
                continue

        if external_id in state:
            skipped += 1
            continue
        in_stock = (product.get("quantity_in_stock") or 0) > 0
        if (
            product.get("presence") != "available"
            or product.get("status") != "on_display"
            or not in_stock
        ):
            logger.info(
                "Skipping %s (%s): not available/on_display/in-stock on Prom.ua (presence=%s, status=%s, qty=%s)",
                external_id,
                name,
                product.get("presence"),
                product.get("status"),
                product.get("quantity_in_stock"),
            )
            skipped += 1
            continue

        if args.limit and processed >= args.limit:
            break
        processed += 1

        try:
            payload = olx_mapper.build_olx_advert_from_prom_product(product)
        except olx_mapper.OlxMappingError as exc:
            logger.warning("Skipping %s (%s): %s", external_id, name, exc)
            failed += 1
            continue

        if args.dry_run:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            continue

        try:
            result = olx_client.create_advert(payload)
        except Exception:
            logger.exception("Failed to create OLX advert for %s (%s)", external_id, name)
            failed += 1
            continue

        advert = result.get("data", result)
        status = advert.get("status")
        entry = {"olx_id": advert.get("id"), "status": status}
        state[external_id] = entry
        _record_synced(external_id, entry)

        if status == "limited":
            limited += 1
            logger.info(
                "%s (%s) created but limited - needs a paid packet to go live, left inactive (no payment made)",
                external_id,
                name,
            )
        else:
            created += 1
            logger.info("%s (%s) -> OLX advert %s (%s)", external_id, name, advert.get("id"), status)

    print(
        f"Done. Active: {created}, limited/needs payment (left inactive, none purchased): {limited}, "
        f"skipped (already synced/unavailable): {skipped}, failed: {failed}"
    )


if __name__ == "__main__":
    main()
