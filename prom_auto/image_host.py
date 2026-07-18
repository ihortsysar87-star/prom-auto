import base64

import requests

from . import config

IMGBB_UPLOAD_URL = "https://api.imgbb.com/1/upload"


def upload_image(image_bytes: bytes) -> str:
    """Uploads a photo to imgbb and returns a permanent direct image URL.

    Prom.ua's import fetches image URLs asynchronously, and Telegram's own
    file links aren't reliable enough to still be alive by the time it does
    (they can 404), so photos are re-hosted here first.
    """
    response = requests.post(
        IMGBB_UPLOAD_URL,
        params={"key": config.IMGBB_API_KEY},
        data={"image": base64.b64encode(image_bytes).decode("utf-8")},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["data"]["url"]
