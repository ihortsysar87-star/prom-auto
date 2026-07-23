from . import article_counter, config


def build_prom_product(data: dict, image_url: str) -> dict:
    """Equivalent of the n8n 'parse to prom api' Code node.

    Maps the structured JSON returned by OpenAI into the Ukrainian column
    names Prom.ua's product import file expects.
    """
    if data.get("error"):
        raise ValueError("Товар не знайдено")

    article = article_counter.next_article()
    keywords = data.get("keywords") or []
    all_keywords = ", ".join(keywords) if isinstance(keywords, list) else ""

    specs = [
        ("Бренд", data.get("brand")),
        ("Модель", data.get("model")),
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
    name = " ".join(part for part in (data.get("name"), data.get("brand")) if part).strip()

    return {
        "Ідентифікатор_товару": article,
        "Код_товару": article,
        # Prom.ua requires non-empty text in "Назва_позиції" (its Russian-
        # language field) or it silently drops the whole row on import - we
        # only have Ukrainian text, so it's duplicated here rather than left
        # blank.
        "Назва_позиції": name,
        "Назва_позиції_укр": name,
        "Опис_укр": description,
        "Кількість": 1,
        "Наявність": "available",
        "Ціна": data.get("priceUAH", 0),
        "Посилання_зображення": image_url,
        "Валюта": "UAH",
        "Одиниця_виміру": "шт",
        "Виробник": data.get("manufacturer", ""),
        "Країна_виробник": data.get("country", ""),
        "Ширина,см": data.get("width", ""),
        "Висота,см": data.get("height", ""),
        "Довжина,см": data.get("length", ""),
        "Вага,кг": data.get("weight", ""),
        "Де_знаходиться_товар": config.PROM_REGION,
        "Пошукові_запити_укр": all_keywords,
    }
