import io

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

# Prom.ua represents each product characteristic as a block of 3 columns
# rather than a single column - "Назва_Характеристики" (name),
# "Одиниця_виміру_Характеристики" (unit, may be blank but the column must
# still be present) and "Значення_Характеристики" (value), in that exact
# order. A row can repeat this block as many times as it has
# characteristics; block position (not header text) is what ties a
# name/unit/value triple together, so the same header text legitimately
# repeats across columns. See product_mapper.build_prom_product's
# "characteristics" key for what feeds this.
CHARACTERISTICS_KEY = "characteristics"
_CHARACTERISTIC_HEADERS = (
    "Назва_Характеристики",
    "Одиниця_виміру_Характеристики",
    "Значення_Характеристики",
)


def _sanitize(value):
    """Strips XML-illegal control characters (e.g. stray \\x0b/\\x0c from a
    scraped source page) that openpyxl otherwise rejects outright with
    IllegalCharacterError, failing the whole batch's xlsx build over a
    single bad character deep in one product's description."""
    if isinstance(value, str):
        return ILLEGAL_CHARACTERS_RE.sub("", value)
    return value


def build_xlsx(products: list[dict]) -> bytes:
    """Equivalent of the n8n 'Convert to File' node (xlsx operation)."""
    wb = Workbook()
    ws = wb.active

    base_headers = [h for h in products[0].keys() if h != CHARACTERISTICS_KEY]
    max_characteristics = max(
        (len(product.get(CHARACTERISTICS_KEY) or []) for product in products), default=0
    )

    headers = list(base_headers)
    for _ in range(max_characteristics):
        headers.extend(_CHARACTERISTIC_HEADERS)
    ws.append(headers)

    for product in products:
        row = [_sanitize(product.get(h, "")) for h in base_headers]
        characteristics = product.get(CHARACTERISTICS_KEY) or []
        for index in range(max_characteristics):
            if index < len(characteristics):
                name, unit, value = characteristics[index]
                row.extend([_sanitize(name), _sanitize(unit or ""), _sanitize(value)])
            else:
                row.extend(["", "", ""])
        ws.append(row)

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
