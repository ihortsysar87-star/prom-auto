import base64
import logging

import requests
from bs4 import BeautifulSoup

from . import config

logger = logging.getLogger(__name__)

VISION_API_URL = "https://vision.googleapis.com/v1/images:annotate"

MAX_MATCHED_PAGES = 3
PAGE_TEXT_MAX_CHARS = 3000

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


def find_web_matches(image_bytes: bytes) -> dict:
    """Reverse image search via Google Cloud Vision's Web Detection feature.

    OpenAI's web_search tool is text-query-only - confirmed against OpenAI's
    own docs - it cannot take an image and find visually similar pages the
    way Google Images/Lens does. It can only search on text the model reads
    off the photo, so a generic product with no distinctive readable
    text/branding is invisible to it even when a human doing a reverse image
    search would find it immediately. This is the real image-matching step.

    Unlike the first version of this module, matched pages' actual text
    content is fetched here too (page_contents), not just their URLs - so
    the model gets real grounding text to confirm against instead of a URL
    it can only re-search by keyword (which doesn't verify the image match
    at all, just restates the same text-search limitation one level down).
    """
    response = requests.post(
        VISION_API_URL,
        params={"key": config.GOOGLE_VISION_API_KEY},
        json={
            "requests": [
                {
                    "image": {"content": base64.b64encode(image_bytes).decode("utf-8")},
                    "features": [{"type": "WEB_DETECTION", "maxResults": 10}],
                }
            ]
        },
        timeout=30,
    )
    response.raise_for_status()
    web_detection = response.json()["responses"][0].get("webDetection", {})

    page_urls = [
        p["url"] for p in web_detection.get("pagesWithMatchingImages", []) if p.get("url")
    ][:5]

    page_contents = {}
    for url in page_urls[:MAX_MATCHED_PAGES]:
        try:
            page_contents[url] = _fetch_page_text(url)
        except Exception:
            logger.warning("Could not fetch matched page %s, skipping its content", url)

    return {
        "page_urls": page_urls,
        "guess_labels": [
            label["label"] for label in web_detection.get("bestGuessLabels", []) if label.get("label")
        ],
        "entity_names": [
            entity["description"]
            for entity in web_detection.get("webEntities", [])
            if entity.get("description")
        ][:5],
        "page_contents": page_contents,
    }


def _fetch_page_text(url: str) -> str:
    response = requests.get(url, headers=_HEADERS, timeout=15)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = " ".join(soup.stripped_strings)
    return text[:PAGE_TEXT_MAX_CHARS]
