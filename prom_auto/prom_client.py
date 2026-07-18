import requests

from . import config

_HEADERS = {"Authorization": f"Bearer {config.PROM_API_TOKEN}"}


def import_file(xlsx_bytes: bytes) -> dict:
    """Equivalent of the n8n 'HTTP Request to prom' node
    (POST /products/import_file, multipart xlsx upload)."""
    response = requests.post(
        f"{config.PROM_API_BASE_URL}/products/import_file",
        headers=_HEADERS,
        files={"file": ("products.xlsx", xlsx_bytes)},
    )
    response.raise_for_status()
    return response.json()
