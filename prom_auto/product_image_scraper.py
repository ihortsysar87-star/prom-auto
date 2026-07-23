import io
import json
import logging
import re

import requests
from bs4 import BeautifulSoup
from PIL import Image

logger = logging.getLogger(__name__)

MAX_IMAGES = 5

# Some storefronts (confirmed on prom.ua) embed the requested thumbnail size
# directly in the image URL and will serve a larger resize on request - e.g.
# ..._w200_h200_....jpg -> ..._w800_h800_....jpg returns a 673x699 image
# instead of a 192x199 one. Rewriting this is a no-op for URLs that don't
# match the pattern, so it's safe to always attempt.
_SIZE_URL_PATTERN = re.compile(r"_w\d+_h\d+_")
_PREFERRED_SIZE = 800

# Below this on either side, a listing photo reads as a thumbnail/placeholder
# rather than a usable product shot - not worth swapping the user's own photo
# for.
MIN_IMAGE_DIMENSION = 400

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


def find_product_image_urls(page_url: str) -> list[str]:
    """Fetches a confirmed marketplace product page (price_source_url from
    openai_client.identify_product) and pulls the seller's own listing
    photos, capped at MAX_IMAGES.

    og:image alone usually only yields a single photo - most storefronts
    (prom.ua included) only ever put the primary image there even when the
    listing has a full gallery. The rest of the gallery is typically only
    in the page's JSON-LD Product schema ("image" field, often a list), so
    that's checked too and merged in.

    Only called once a real price match is confirmed, so the page is known
    to be the actual product, not a guess.
    """
    response = requests.get(page_url, headers=_HEADERS, timeout=15)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    urls = []
    for tag in soup.find_all("meta", property="og:image"):
        content = tag.get("content")
        if content and content not in urls:
            urls.append(content)

    for url in _extract_json_ld_images(soup):
        if url not in urls:
            urls.append(url)

    upsized = []
    for url in urls:
        upsized_url = _upsize_url(url)
        if upsized_url not in upsized:
            upsized.append(upsized_url)
    return upsized[:MAX_IMAGES]


def _upsize_url(url: str) -> str:
    return _SIZE_URL_PATTERN.sub(f"_w{_PREFERRED_SIZE}_h{_PREFERRED_SIZE}_", url)


def _extract_json_ld_images(soup: BeautifulSoup) -> list[str]:
    urls = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        for entry in data if isinstance(data, list) else [data]:
            if not isinstance(entry, dict):
                continue
            image = entry.get("image")
            if isinstance(image, str):
                urls.append(image)
            elif isinstance(image, list):
                urls.extend(u for u in image if isinstance(u, str))
    return urls


def fetch_image_bytes(image_url: str) -> bytes:
    response = requests.get(image_url, headers=_HEADERS, timeout=15)
    response.raise_for_status()
    return response.content


def is_acceptable_quality(image_bytes: bytes) -> bool:
    """Rejects thumbnails/placeholders and anything PIL can't even decode -
    those are worse than the user's own phone photo, not better."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            width, height = img.size
    except Exception:
        logger.warning("Scraped image is not a decodable image, rejecting")
        return False

    if width < MIN_IMAGE_DIMENSION or height < MIN_IMAGE_DIMENSION:
        logger.info("Scraped image %dx%d below minimum %dpx, rejecting", width, height, MIN_IMAGE_DIMENSION)
        return False
    return True
