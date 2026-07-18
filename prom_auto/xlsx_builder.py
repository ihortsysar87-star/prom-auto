import io

from openpyxl import Workbook


def build_xlsx(products: list[dict]) -> bytes:
    """Equivalent of the n8n 'Convert to File' node (xlsx operation)."""
    wb = Workbook()
    ws = wb.active

    headers = list(products[0].keys())
    ws.append(headers)
    for product in products:
        ws.append([product.get(h, "") for h in headers])

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
