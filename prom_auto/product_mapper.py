import random


def build_prom_product(data: dict, image_url: str) -> dict:
    """Equivalent of the n8n 'parse to prom api' Code node.

    Maps the structured JSON returned by OpenAI into the Ukrainian column
    names Prom.ua's product import file expects.
    """
    if data.get("error"):
        raise ValueError("Товар не знайдено")

    sku = random.randint(1000, 9999)
    keywords = data.get("keywords") or []
    all_keywords = ", ".join(keywords) if isinstance(keywords, list) else ""

    specs = [
        ("Матеріал", data.get("material")),
        ("Колір", data.get("color")),
        ("Ширина", data.get("width")),
        ("Висота", data.get("height")),
        ("Довжина", data.get("length")),
        ("Вага", data.get("weight")),
    ]
    specifications = "".join(
        f"- {label}: {value};\n" for label, value in specs if value and value != "null"
    )

    description = f"{data.get('description', '')}\n\nХарактеристики:\n{specifications}"
    name = f"{data.get('name', '')} {data.get('brand', '')}".strip()

    return {
        "Унікальний_ідентифікатор": sku,
        "Код_товару": f"v{sku}",
        "Назва_позиції": name,
        "Назва_позиції_укр": name,
        "Опис_укр": description,
        "Кількість": 1,
        "Наявність": "available",
        "Ціна": data.get("priceUAH", 0),
        "Посилання_зображення": image_url,
        "Валюта": "UAH",
        "Одиниця_виміру": "шт",
        "Бренд": data.get("brand", ""),
        "Виробник": data.get("manufacturer", ""),
        "Країна_виробник": data.get("country", ""),
        "Матеріал": data.get("material", ""),
        "Ширина,см": data.get("width", ""),
        "Висота,см": data.get("height", ""),
        "Довжина,см": data.get("length", ""),
        "Вага,кг": data.get("weight", ""),
        "Модель": data.get("model", ""),
        "Пошукові_запити_укр": all_keywords,
    }
