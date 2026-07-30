import logging
import re
import urllib.parse

from telegram import InputMediaPhoto, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from . import (
    config,
    image_host,
    openai_client,
    page_fetch,
    product_data_extractor,
    product_image_scraper,
    prom_client,
    product_mapper,
    xlsx_builder,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Photos sharing a Telegram media_group_id (sent together as one album) are
# treated as multiple photos of the SAME product; a photo sent on its own is
# its own product. Telegram delivers each album photo as a separate update,
# so this is how long to wait after the last photo of a group before
# deciding the group is complete and asking whether to add another product.
ALBUM_COLLECT_DELAY = 2.5

# chat_id -> {
#   "groups": list[list[bytes]],           completed product photo-groups,
#                                           queued for _process_batch
#   "current_group": list[bytes] | None,   photos being collected for the
#                                           in-progress group
#   "current_media_group_id": str | None,  media_group_id of the in-progress
#                                           group, if any
#   "job": Job | None,                     debounce job that finalizes
#                                           current_group
#   "awaiting_continue": bool,             True once we've asked "add
#                                           another product?"
# }
_pending_batches: dict[int, dict] = {}

# chat_id -> {"queue": list[tuple[list[bytes], dict]], "awaiting": bool}. Drives the
# post-batch "what price for the sales group?" back-and-forth: one entry per
# successfully identified product, asked one at a time.
_pending_price_requests: dict[int, dict] = {}

# chat_id -> "awaiting_choice" | "url" | "photo". Set by /start (prompts the
# link-vs-photo choice) and by the yes/no answer to it; drives which branch
# handle_text/handle_photo take for that chat. Missing entirely == legacy
# behavior (bare photo mode, no prompt), so chats that never ran /start keep
# working exactly as before.
_chat_mode: dict[int, str] = {}

# chat_id -> {"products": list[dict], "data_list": list[dict]}. Link-mode's
# equivalent of _pending_batches: one entry per successfully identified URL,
# collected until the user sends a _DONE_ANSWERS reply.
_pending_url_batches: dict[int, dict] = {}

# Ukrainian/English yes-no answers, used both for "add another product?" and
# for the per-product sales-group prompt.
_YES_ANSWERS = {"так", "да", "yes", "y"}
_NO_ANSWERS = {"ні", "ни", "нет", "no", "n"}

# Marks the end of a link-mode batch (user has no more URLs to send).
_DONE_ANSWERS = {"готово", "стоп", "done", "кінець", "закінчити"}

_MODE_QUESTION = (
    "Вітаю! Як бажаєте передати товари?\n\n"
    "🔗 Посиланнями на товар (рекомендовано - швидше і дешевше) - напишіть «так»\n"
    "📸 Фотографіями, як зазвичай - напишіть «ні»"
)

_FIELD_LABELS = {
    "name": "назва",
    "brand": "бренд",
    "manufacturer": "виробник",
    "country": "країна",
    "material": "матеріал",
    "color": "колір",
}


def _format_data_sources(data_sources: dict, source_urls: dict | None = None) -> str:
    """Groups fields by whether they came from reading the photo, an
    actual web match, or the model's own estimate, so it's clear what's
    verified vs. just read off the packaging. Web-sourced fields get their
    backing URL appended so the claim is checkable, not just self-reported."""
    groups: dict[str, list[str]] = {"web": [], "photo": [], "estimated": []}
    source_urls = source_urls or {}
    for field, source in (data_sources or {}).items():
        label = _FIELD_LABELS.get(field, field)
        if source == "web" and source_urls.get(field):
            label = f"{label} ({source_urls[field]})"
        groups.setdefault(source, []).append(label)

    parts = []
    if groups["web"]:
        parts.append("з пошуку: " + ", ".join(groups["web"]))
    if groups["photo"]:
        parts.append("з фото: " + ", ".join(groups["photo"]))
    if groups["estimated"]:
        parts.append("орієнтовно: " + ", ".join(groups["estimated"]))
    return "ℹ️ Джерела даних — " + "; ".join(parts) if parts else ""


def _normalize_url(url: str) -> str:
    """Strips query string/fragment/trailing-slash so a citation URL and the
    (possibly slightly reformatted) copy of it the model writes into
    source_urls still compare equal - e.g. a tracking param or trailing
    slash shouldn't make a genuinely cited page look uncited."""
    parsed = urllib.parse.urlsplit(url.strip())
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def _normalize_for_match(text: str) -> str:
    """Lowercases and collapses everything but letters/digits to single
    spaces, so "iPhone 15 Pro Max" and "iPhone-15 Pro/Max!!" (real-world
    punctuation/spacing drift between what the model wrote and what's
    literally in a page's HTML) compare equal instead of failing a naive
    substring check."""
    return " ".join(re.findall(r"[\w']+", text.lower()))


def _anchor_words_present(anchor_normalized: str, text_normalized: str) -> bool:
    """True if every significant (3+ char) word of the anchor shows up
    somewhere in the page text, order-independent. Looser than an exact
    substring match (which real pages routinely fail on for trivial
    formatting reasons) while still requiring genuine word-level overlap."""
    words = [w for w in anchor_normalized.split() if len(w) >= 3]
    if not words:
        return anchor_normalized and anchor_normalized in text_normalized
    return all(w in text_normalized for w in words)


def _identify_and_validate(photos: list[bytes]) -> tuple[dict, int]:
    """Runs identify_product once and enforces that any "web" data_source
    claim is backed by a real URL the model's own search tool actually
    visited, downgrading to "estimated" otherwise - a "web" self-report is
    only as trustworthy as the evidence behind it, regardless of how many
    searches ran.

    Returns (data, search_count).
    """
    response, search_count, cited_urls = openai_client.identify_product(photos)
    data = openai_client.extract_json(response)
    logger.info("Full identification data (%d web search(es)): %s", search_count, data)

    data_sources = data.get("data_sources") or {}
    source_urls = data.get("source_urls") or {}
    data["source_urls"] = source_urls  # keep linked even if data had no source_urls at all
    if data.get("price_found") and not (data.get("price_source_url") or "").strip():
        logger.warning("price_found=True with no price_source_url - downgrading")
        data["price_found"] = False

    # A field marked "web" with no URL of its own isn't necessarily
    # unconfirmed - one confirmed listing page backs every field it
    # describes (name, brand, material, ...), and the model routinely only
    # bothers to write the URL once (as price_source_url, or against
    # whichever single field it considered primary) rather than repeating it
    # for every other field it also read off the same page. Treating that as
    # "no evidence" was downgrading fields that actually did have real
    # backing - fall back to any other confirmed URL already on this product
    # before giving up on it.
    fallback_url = next(
        (u.strip() for u in [data.get("price_source_url"), *source_urls.values()] if u and u.strip()),
        "",
    )
    for field, source in data_sources.items():
        if source == "web" and not (source_urls.get(field) or "").strip():
            if fallback_url:
                source_urls[field] = fallback_url
            else:
                logger.warning("Field %r marked 'web' with no source_urls entry - downgrading", field)
                data_sources[field] = "estimated"

    # Two layers of evidence for a "web" claim, from strongest to weakest:
    #   1. The URL must be one the model's search tool actually retrieved
    #      (a real url_citation), not just a plausible-looking string it
    #      typed into the JSON - this is the main defense against
    #      hallucinated sources, and it's real evidence we already had but
    #      used to throw away.
    #   2. IF we can independently re-fetch that page ourselves AND the
    #      product has a brand/model to check for, the anchor should appear
    #      in it (fuzzy, word-level - see _anchor_words_present) as extra
    #      corroboration. But failing to fetch (bot-blocked, JS-rendered
    #      page) or having no brand/model at all (common for generic,
    #      unbranded products) must NOT be treated as "wrong" - only a
    #      genuinely uncited URL is. Conflating "couldn't independently
    #      verify" with "verified wrong" was the main reason legitimate
    #      matches for brandless products or bot-protected sites were being
    #      silently downgraded to "not found".
    #
    #   Brand and model are checked as SEPARATE candidate anchors (match on
    #   either, not just model-first-else-brand) - many store-brand/private
    #   label products (e.g. Lidl's "Ernesto" line) have a "model" that's
    #   really the manufacturer's internal packaging code (an IAN number in
    #   Ernesto's case), which legitimate third-party listings never
    #   reprint verbatim even for a correct match, while the brand name
    #   almost always does appear. Requiring the model specifically to
    #   match was silently downgrading correct matches for exactly these
    #   products.
    brand_anchor = _normalize_for_match((data.get("brand") or "").strip())
    model_anchor = _normalize_for_match((data.get("model") or "").strip())
    anchors = [a for a in (brand_anchor, model_anchor) if a]
    normalized_cited = {_normalize_url(u) for u in cited_urls}
    page_text_cache: dict[str, str | None] = {}

    def _is_cited(url: str) -> bool:
        return _normalize_url(url) in normalized_cited

    def _fetch_confirms_anchor(url: str) -> bool | None:
        """None means "couldn't check" (no anchor to check, or fetch
        failed) - distinct from True/False so callers don't mistake
        "unknown" for "disproven"."""
        if not anchors:
            return None
        if url not in page_text_cache:
            try:
                page_text_cache[url] = page_fetch.fetch_page_text(url)
            except Exception:
                logger.info("Could not independently fetch %s (bot-blocked/JS page?) - relying on citation alone", url)
                page_text_cache[url] = None
        text = page_text_cache[url]
        if text is None:
            return None
        normalized_text = _normalize_for_match(text)
        return any(_anchor_words_present(a, normalized_text) for a in anchors)

    def _validate_url(field_label: str, url: str) -> bool:
        if not url or not _is_cited(url):
            logger.warning(
                "%s cites %s but that URL was never actually retrieved by web_search - downgrading",
                field_label,
                url,
            )
            return False
        confirmed = _fetch_confirms_anchor(url)
        if confirmed is False:
            logger.warning(
                "%s cites %s (a real search result) but none of anchors %r found on independent re-fetch - downgrading",
                field_label,
                url,
                anchors,
            )
            return False
        return True

    for field, source in list(data_sources.items()):
        if source != "web":
            continue
        url = (source_urls.get(field) or "").strip()
        if not _validate_url(f"Field {field!r}", url):
            data_sources[field] = "estimated"

    if data.get("price_found"):
        price_url = (data.get("price_source_url") or "").strip()
        if not _validate_url("price_source_url", price_url):
            data["price_found"] = False

    # Distinct from price_found: whether the PRODUCT ITSELF was confirmed by
    # a real, validated web match, regardless of whether that page happened
    # to list a price. Manufacturer pages, spec sheets, and catalog listings
    # routinely confirm the exact product with no price on them at all -
    # treating "no price on the page" the same as "product not found" was
    # the main reason correctly-identified products were reported to the
    # user as unconfirmed. Both sides here only ever hold URLs that already
    # passed _validate_url above (price_source_url only counts if
    # price_found is still True; data_sources[f] == "web" only survives for
    # fields that passed the loop above).
    price_confirmed_url = (data.get("price_source_url") or "").strip() if data.get("price_found") else ""
    field_confirmed_url = next(
        (source_urls[f] for f, s in data_sources.items() if s == "web" and (source_urls.get(f) or "").strip()),
        "",
    )
    data["confirmed_source_url"] = price_confirmed_url or field_confirmed_url or None

    return data, search_count


async def _identify_single_product(
    bot, chat_id: int, photos: list[bytes], index: int, total: int
) -> tuple[dict, dict] | None:
    """Runs the full identify -> validate -> (maybe swap in marketplace
    photos) pipeline for one product (one or more photos of it), sending
    progress messages prefixed with its position in the batch. Returns
    (prom_row, data), or None if this particular item failed - callers
    should skip it and keep processing the rest of the batch rather than
    aborting everything.
    """
    tag = f"[{index}/{total}]" if total > 1 else ""
    prefix = f"{tag} " if tag else ""

    try:
        # Re-host on imgbb: Telegram's own file links can 404 by the time
        # Prom.ua's async import gets around to fetching them. All of this
        # product's photos are uploaded and passed to Prom.ua as one
        # comma-separated field (its import format's convention for
        # multiple images per listing).
        image_urls = [image_host.upload_image(photo) for photo in photos]
        image_url = ", ".join(image_urls)

        await bot.send_message(chat_id=chat_id, text=f"{prefix}🔍 Розпізнаю товар...")
        data, _search_count = _identify_and_validate(photos)

        await bot.send_message(
            chat_id=chat_id,
            text=f"{prefix}🔍 Товар розпізнано: «{data.get('name', '')}». Формую картку для Prom.ua...",
        )
        sources_text = _format_data_sources(data.get("data_sources"), data.get("source_urls"))
        if sources_text:
            await bot.send_message(chat_id=chat_id, text=f"{prefix}{sources_text}")

        # confirmed_source_url (set in _identify_and_validate) means the
        # PRODUCT ITSELF was confirmed by a real, validated web match - this
        # is intentionally NOT the same gate as price_found, since a page
        # can confirm the exact product (manufacturer site, spec sheet,
        # catalog listing) with no price on it at all. Conflating the two
        # was reporting correctly-identified products to the user as
        # "not found" just because that particular page had no price.
        confirmed_url = data.get("confirmed_source_url")

        if data.get("price_found") and data.get("price_source_url"):
            await bot.send_message(
                chat_id=chat_id,
                text=f"{prefix}💰 Ціну підтверджено тут: {data['price_source_url']}",
            )

        if confirmed_url:
            # Swap the user's own snapshot for the seller's/manufacturer's
            # own listing photos, which are almost always better
            # quality/lighting. Every fallback-to-own-photo path below
            # tells the user why, instead of silently keeping the original
            # with no explanation.
            try:
                marketplace_image_urls = product_image_scraper.find_product_image_urls(confirmed_url)
            except Exception:
                logger.exception(
                    "Failed to scrape marketplace photos from %s, keeping own photo(s)",
                    confirmed_url,
                )
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"{prefix}ℹ️ Не вдалося завантажити сторінку джерела для фото — "
                        "залишаю ваше власне фото."
                    ),
                )
                marketplace_image_urls = []

            if not marketplace_image_urls:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"{prefix}ℹ️ На підтвердженій сторінці не знайдено фото товару — "
                        "залишаю ваше власне фото."
                    ),
                )

            good_image_bytes = []
            for url in marketplace_image_urls:
                try:
                    img_bytes = product_image_scraper.fetch_image_bytes(url)
                except Exception:
                    logger.exception("Failed to download scraped image %s, skipping", url)
                    continue
                if product_image_scraper.is_acceptable_quality(img_bytes):
                    good_image_bytes.append(img_bytes)

            if good_image_bytes:
                try:
                    rehosted_urls = [image_host.upload_image(b) for b in good_image_bytes]
                    image_url = ", ".join(rehosted_urls)
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"{prefix}🖼️ Використовую {len(rehosted_urls)} фото товару з "
                            "підтвердженого джерела замість власного знімку."
                        ),
                    )
                except Exception:
                    logger.exception(
                        "Failed to re-host scraped marketplace photos, keeping own photo(s)"
                    )
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"{prefix}ℹ️ Знайдені фото не вдалося завантажити на хостинг — "
                            "залишаю ваше власне фото."
                        ),
                    )
            elif marketplace_image_urls:
                logger.info(
                    "All %d scraped image(s) failed the quality check, keeping own photo(s)",
                    len(marketplace_image_urls),
                )
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"{prefix}ℹ️ Знайдено {len(marketplace_image_urls)} фото товару на джерелі, "
                        "але вони замалі за розміром (нижче 400px) — залишаю ваше власне фото."
                    ),
                )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"{prefix}⚠️ Пошук не підтвердив саме цей товар (ціну поставлено як заглушку "
                    f"{data.get('priceUAH')} грн). Перевірте назву, характеристики та ціну вручну."
                ),
            )

        return product_mapper.build_prom_product(data, image_url), data
    except Exception as exc:
        logger.exception("Failed to process product %d/%d", index, total)
        await bot.send_message(chat_id=chat_id, text=f"{prefix}❌ Помилка обробки товару: {exc}")
        return None


async def _process_batch(bot, chat_id: int, groups: list[list[bytes]]) -> None:
    """Identifies every photo group in the batch as its own separate
    product, then combines all of them into a single Prom.ua import - one
    API call for the whole batch instead of one per product, since Prom.ua's
    import endpoint is flaky/rate-limited under repeated back-to-back
    calls."""
    total = len(groups)
    photo_count = sum(len(group) for group in groups)
    photo_word = "фото" if photo_count == 1 else f"{photo_count} фото"
    product_word = "товар" if total == 1 else f"{total} товар(и/ів)"
    await bot.send_message(
        chat_id=chat_id,
        text=f"📸 Отримано {photo_word} ({product_word}). Обробляю по черзі...",
    )

    products = []
    identified = []  # (photos, data) pairs, for the sales-group price queue below
    for index, photos in enumerate(groups, start=1):
        result = await _identify_single_product(bot, chat_id, photos, index, total)
        if result is not None:
            product, data = result
            products.append(product)
            identified.append((photos, data))

    if not products:
        await bot.send_message(chat_id=chat_id, text="❌ Жоден товар не вдалося розпізнати, нічого завантажувати.")
        return

    xlsx_bytes = xlsx_builder.build_xlsx(products)

    await bot.send_message(
        chat_id=chat_id,
        text=f"⬆️ Завантажую {len(products)} товар(и/ів) до Prom.ua одним запитом...",
    )
    try:
        result = prom_client.import_file(xlsx_bytes)
        logger.info("Prom.ua import result: %s", result)
        if result.get("status") == "success" or "id" in result:
            await bot.send_message(
                chat_id=chat_id, text=f"✅ {len(products)} товар(и/ів) успішно передано в Пром"
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=f"❌ Помилка передачі на Пром: {result.get('error', '')}",
            )
    except prom_client.PromImportBusyError:
        logger.warning("Prom.ua import busy after retries (likely nightly restriction)")
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "⏳ Prom.ua зараз не приймає імпорт (схоже на їхнє нічне обмеження). "
                "Це не помилка бота — спробуйте надіслати фото ще раз пізніше."
            ),
        )
    except Exception as exc:
        logger.exception("Prom.ua import failed")
        await bot.send_message(chat_id=chat_id, text=f"❌ Помилка передачі на Пром: {exc}")

    # Prom.ua's own outcome doesn't gate the sales-group post - that's a
    # separate customer-facing channel with its own manually-set price, not
    # tied to whether the Prom.ua import itself succeeded.
    if identified and config.SALES_GROUP_CHAT_ID:
        _pending_price_requests[chat_id] = {"queue": identified, "awaiting": False}
        await bot.send_message(
            chat_id=chat_id,
            text="💬 Тепер оберіть, які товари додати в групу продажів (по одному, у відповідь на запит):",
        )
        await _ask_next_price(bot, chat_id)


async def _ask_next_price(bot, chat_id: int) -> None:
    state = _pending_price_requests.get(chat_id)
    if not state or not state["queue"]:
        _pending_price_requests.pop(chat_id, None)
        return
    _photos, data = state["queue"][0]
    state["awaiting"] = True
    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"💬 Додати «{data.get('name', '')}» в групу продажів? "
            "Якщо так - напишіть ціну, якщо ні - напишіть «ні»."
        ),
    )


async def _post_to_sales_group(bot, chat_id: int, photos: list[bytes], data: dict, price: float) -> None:
    caption = (
        f"{data.get('name', '')}\n\n"
        f"Ціна: {price:.0f} грн\n\n"
        f"Щоб купити, напишіть нашому менеджеру: {config.MANAGER_CONTACT_URL}"
    )
    try:
        if len(photos) > 1:
            # sendMediaGroup only shows the caption of the first item as the
            # album's caption - the rest are left uncaptioned on purpose.
            media = [
                InputMediaPhoto(photo, caption=caption if i == 0 else None)
                for i, photo in enumerate(photos)
            ]
            await bot.send_media_group(chat_id=config.SALES_GROUP_CHAT_ID, media=media)
        else:
            await bot.send_photo(chat_id=config.SALES_GROUP_CHAT_ID, photo=photos[0], caption=caption)
        await bot.send_message(chat_id=chat_id, text="✅ Додано в групу продажів")
    except Exception as exc:
        logger.exception("Failed to post to sales group")
        await bot.send_message(chat_id=chat_id, text=f"❌ Не вдалося додати в групу продажів: {exc}")


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.message.chat.id
    _chat_mode[chat_id] = "awaiting_choice"
    _pending_url_batches.pop(chat_id, None)
    await context.bot.send_message(chat_id=chat_id, text=_MODE_QUESTION)


async def _identify_single_url_product(bot, chat_id: int, url: str, index: int) -> dict | None:
    """Runs the link-mode identify -> price/discount -> images pipeline for
    one URL, sending progress messages prefixed with its position in the
    batch. Returns the Prom.ua row dict, or None if this URL failed -
    callers should skip it and keep processing the rest of the batch."""
    tag = f"[{index}]"

    await bot.send_message(chat_id=chat_id, text=f"{tag} 🔍 Обробляю посилання...")
    try:
        data, image_urls = product_data_extractor.identify_product_from_url(url)
    except product_data_extractor.ProductNotFoundError as exc:
        await bot.send_message(chat_id=chat_id, text=f"{tag} ❌ Не вдалося розпізнати товар: {exc}")
        return None
    except Exception as exc:
        logger.exception("Failed to process URL %s", url)
        await bot.send_message(chat_id=chat_id, text=f"{tag} ❌ Не вдалося обробити посилання: {exc}")
        return None

    if not image_urls:
        await bot.send_message(
            chat_id=chat_id,
            text=f"{tag} ℹ️ На сторінці не знайдено фото товару - картка буде без зображення.",
        )

    if not data.get("price_found"):
        await bot.send_message(
            chat_id=chat_id,
            text=f"{tag} ⚠️ Ціну на сторінці не знайдено - перевірте та встановіть її вручну після завантаження.",
        )

    try:
        prom_row = product_mapper.build_prom_product(data, ", ".join(image_urls))
    except ValueError as exc:
        await bot.send_message(chat_id=chat_id, text=f"{tag} ❌ {exc}")
        return None

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"{tag} ✅ «{data.get('name', '')}» - {prom_row['Ціна']} грн "
            "(знижка 5% від ціни джерела вже враховано). Надішліть наступне посилання, або напишіть «готово»."
        ),
    )
    return prom_row


async def _handle_url_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat.id
    text = update.message.text.strip()
    batch = _pending_url_batches.setdefault(chat_id, {"products": [], "data_list": []})

    if text.lower() in _DONE_ANSWERS:
        products = batch["products"]
        _pending_url_batches.pop(chat_id, None)
        _chat_mode.pop(chat_id, None)
        if not products:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ Жодного товару не додано, нічого завантажувати.")
            return
        await _finalize_url_batch(context.bot, chat_id, products)
        return

    if not text.lower().startswith(("http://", "https://")):
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Це не схоже на посилання. Надішліть URL товару, або напишіть «готово», щоб завершити.",
        )
        return

    index = len(batch["products"]) + 1
    row = await _identify_single_url_product(context.bot, chat_id, text, index)
    if row is not None:
        batch["products"].append(row)


async def _finalize_url_batch(bot, chat_id: int, products: list[dict]) -> None:
    """Same tail as _process_batch: one xlsx, one Prom.ua import call for
    the whole link-mode batch."""
    xlsx_bytes = xlsx_builder.build_xlsx(products)
    await bot.send_message(
        chat_id=chat_id,
        text=f"⬆️ Завантажую {len(products)} товар(и/ів) до Prom.ua одним запитом...",
    )
    try:
        result = prom_client.import_file(xlsx_bytes)
        logger.info("Prom.ua import result: %s", result)
        if result.get("status") == "success" or "id" in result:
            await bot.send_message(
                chat_id=chat_id, text=f"✅ {len(products)} товар(и/ів) успішно передано в Пром"
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=f"❌ Помилка передачі на Пром: {result.get('error', '')}",
            )
    except prom_client.PromImportBusyError:
        logger.warning("Prom.ua import busy after retries (likely nightly restriction)")
        await bot.send_message(
            chat_id=chat_id,
            text=(
                "⏳ Prom.ua зараз не приймає імпорт (схоже на їхнє нічне обмеження). "
                "Це не помилка бота - спробуйте /start ще раз пізніше."
            ),
        )
    except Exception as exc:
        logger.exception("Prom.ua import failed")
        await bot.send_message(chat_id=chat_id, text=f"❌ Помилка передачі на Пром: {exc}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    chat_id = update.message.chat.id
    # Logged for every text message (not just ones we act on) so the sales
    # group's chat_id can be read straight from the logs once the bot is
    # added there and someone sends any message - negative IDs are groups.
    logger.info(
        "Text message from chat %s (%r, type=%s): %r",
        chat_id,
        update.message.chat.title,
        update.message.chat.type,
        update.message.text[:200],
    )

    answer = update.message.text.strip()
    answer_lower = answer.lower()

    mode = _chat_mode.get(chat_id)
    if mode == "awaiting_choice":
        if answer_lower in _YES_ANSWERS:
            _chat_mode[chat_id] = "url"
            _pending_url_batches[chat_id] = {"products": [], "data_list": []}
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🔗 Добре, надсилайте посилання на товари одне за одним (по одному в повідомленні). "
                    "Коли завершите - напишіть «готово»."
                ),
            )
        elif answer_lower in _NO_ANSWERS:
            _chat_mode[chat_id] = "photo"
            await context.bot.send_message(chat_id=chat_id, text="📸 Добре, надсилайте фото товарів одне за одним.")
        else:
            await context.bot.send_message(
                chat_id=chat_id, text="⚠️ Не зрозумів відповідь. Напишіть «так» або «ні»."
            )
        return

    if mode == "url":
        await _handle_url_message(update, context)
        return

    batch = _pending_batches.get(chat_id)
    if batch and batch.get("awaiting_continue"):
        if answer_lower in _YES_ANSWERS:
            batch["awaiting_continue"] = False
            await context.bot.send_message(chat_id=chat_id, text="👍 Надсилайте фото наступного товару.")
        elif answer_lower in _NO_ANSWERS:
            groups = batch["groups"]
            _pending_batches.pop(chat_id, None)
            await _process_batch(context.bot, chat_id, groups)
        else:
            await context.bot.send_message(
                chat_id=chat_id, text="⚠️ Не зрозумів відповідь. Напишіть «так» або «ні»."
            )
        return

    state = _pending_price_requests.get(chat_id)
    if not state or not state.get("awaiting"):
        return

    if answer_lower in _NO_ANSWERS:
        _photos, data = state["queue"].pop(0)
        state["awaiting"] = False
        await context.bot.send_message(
            chat_id=chat_id, text=f"⏭️ Пропущено «{data.get('name', '')}», не додано в групу продажів"
        )
        await _ask_next_price(context.bot, chat_id)
        return

    try:
        price = float(answer.replace(",", "."))
    except ValueError:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Не розпізнав відповідь. Напишіть ціну числом, або «ні», щоб не додавати товар.",
        )
        return

    photos, data = state["queue"].pop(0)
    state["awaiting"] = False
    await _post_to_sales_group(context.bot, chat_id, photos, data, price)
    await _ask_next_price(context.bot, chat_id)


async def _finalize_current_group(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.data
    batch = _pending_batches.get(chat_id)
    if not batch or not batch["current_group"]:
        return

    batch["groups"].append(batch["current_group"])
    photo_count = len(batch["current_group"])
    batch["current_group"] = None
    batch["current_media_group_id"] = None
    batch["job"] = None
    batch["awaiting_continue"] = True

    photo_word = "фото" if photo_count == 1 else f"{photo_count} фото"
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"📦 Товар #{len(batch['groups'])} додано до черги ({photo_word}). "
            "Додати ще один товар? (так/ні)"
        ),
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return

    chat_id = update.message.chat.id

    if _chat_mode.get(chat_id) == "url":
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Ви обрали режим посилань. Надішліть URL товару текстом, або напишіть «готово», щоб завершити.",
        )
        return

    media_group_id = update.message.media_group_id
    logger.info("Photo received from chat %s (media_group_id=%s)", chat_id, media_group_id)

    photo = update.message.photo[-1]
    photo_file = await photo.get_file()
    image_bytes = bytes(await photo_file.download_as_bytearray())

    # Photos sharing a media_group_id (sent together as one Telegram album)
    # are collected as multiple photos of the SAME product; anything else
    # (a lone photo, or one whose media_group_id differs from the group
    # currently being collected) starts a new product.
    batch = _pending_batches.setdefault(
        chat_id,
        {
            "groups": [],
            "current_group": None,
            "current_media_group_id": None,
            "job": None,
            "awaiting_continue": False,
        },
    )

    # A photo arriving while we're waiting on "add another product?" starts
    # the next product implicitly - no need to make the user type "так" first.
    batch["awaiting_continue"] = False

    same_group = (
        batch["current_group"] is not None
        and media_group_id is not None
        and media_group_id == batch["current_media_group_id"]
    )
    if same_group:
        batch["current_group"].append(image_bytes)
    else:
        if batch["current_group"]:
            batch["groups"].append(batch["current_group"])
        batch["current_group"] = [image_bytes]
        batch["current_media_group_id"] = media_group_id

    if batch["job"] is not None:
        batch["job"].schedule_removal()
    batch["job"] = context.job_queue.run_once(_finalize_current_group, ALBUM_COLLECT_DELAY, data=chat_id)


def main() -> None:
    if not config.TELEGRAM_TOKEN:
        print("Please set the TELEGRAM_TOKEN environment variable before running the bot.")
        return

    application = Application.builder().token(config.TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", handle_start))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.run_polling()


if __name__ == "__main__":
    main()
