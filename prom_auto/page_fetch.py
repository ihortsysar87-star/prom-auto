import logging

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

PAGE_TEXT_MAX_CHARS = 3000

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# Public read-only "render and return clean text" proxy, used as a fallback
# when a marketplace's own bot-protection (Cloudflare's JS challenge is the
# common case - confirmed on Rozetka) blocks a direct fetch outright. No
# request bodies or credentials ever go through it, just the public product
# page URL itself.
_READER_PROXY_PREFIX = "https://r.jina.ai/"

_CHALLENGE_MARKERS = ("just a moment", "attention required", "cf-chl", "checking your browser")


def _looks_like_bot_challenge(html: str) -> bool:
    lowered = html[:2000].lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def fetch_html(url: str) -> tuple[str, bool]:
    """Fetches a product page for structured extraction. Returns
    (content, is_html): is_html is True for a normal direct fetch (raw HTML,
    safe to parse for JSON-LD/meta tags), and False when the direct fetch
    was blocked and the reader-proxy fallback had to be used instead (it
    returns rendered text/markdown, not HTML - no JSON-LD to parse there).
    """
    try:
        response = requests.get(url, headers=_HEADERS, timeout=20)
        if response.status_code != 403 and not _looks_like_bot_challenge(response.text):
            response.raise_for_status()
            return response.text, True
    except requests.RequestException:
        logger.info("Direct fetch of %s failed, falling back to reader proxy", url)

    logger.info("Falling back to reader proxy for %s", url)
    response = requests.get(_READER_PROXY_PREFIX + url, headers=_HEADERS, timeout=30)
    response.raise_for_status()
    return response.text, False


def fetch_page_text(url: str) -> str:
    response = requests.get(url, headers=_HEADERS, timeout=15)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = " ".join(soup.stripped_strings)
    return text[:PAGE_TEXT_MAX_CHARS]
