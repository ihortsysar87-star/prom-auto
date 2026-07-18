import base64

import requests

from . import config

VISION_API_URL = "https://vision.googleapis.com/v1/images:annotate"


def find_web_matches(image_bytes: bytes) -> dict:
    """Reverse image search via Google Cloud Vision's Web Detection feature.

    OpenAI's web_search tool only does text search on a query the model
    writes itself - it can't match the photo against the web directly. This
    finds actual pages containing this photo (or visually similar ones) so
    the identification step has real leads to verify instead of guessing.
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

    return {
        "page_urls": [
            p["url"] for p in web_detection.get("pagesWithMatchingImages", []) if p.get("url")
        ][:5],
        "guess_labels": [
            label["label"] for label in web_detection.get("bestGuessLabels", []) if label.get("label")
        ],
        "entity_names": [
            entity["description"]
            for entity in web_detection.get("webEntities", [])
            if entity.get("description")
        ][:5],
    }
