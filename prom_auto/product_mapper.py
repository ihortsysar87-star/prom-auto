from . import article_counter, config, xlsx_builder

_CHARACTERISTIC_UNITS = {"Ширина": "см", "Висота": "см", "Довжина": "см", "Вага": "кг"}


def build_characteristics(data: dict) -> list[tuple[str, str, object]]:
    """Builds Prom.ua's structured (name, unit, value) characteristic
    triples (see xlsx_builder) from the same brand/model/material/color/
    dimensions fields build_prom_product() also bakes into the free-text
    description. Exposed separately so rozetka_backfill.py can produce the
    same triples for products that already exist on Prom.ua, without
    rebuilding a whole product row for them."""
    return [
        (label, _CHARACTERISTIC_UNITS.get(label, ""), value)
        for label, value in (
            ("Бренд", data.get("brand")),
            ("Модель", data.get("model")),
            ("Матеріал", data.get("material")),
            ("Колір", data.get("color")),
            ("Ширина", data.get("width")),
            ("Висота", data.get("height")),
            ("Довжина", data.get("length")),
            ("Вага", data.get("weight")),
        )
        if value and value != "null"
    ]


def build_prom_product(data: dict, image_url: str) -> dict:
    """Equivalent of the n8n 'parse to prom api' Code node.

    Maps the structured JSON returned by OpenAI into the Ukrainian column
    names Prom.ua's product import file expects.
    """
    if data.get("error"):
        raise ValueError("Товар не знайдено")

    article = article_counter.next_article()
    external_id = article_counter.external_id_for(article)
    keywords = data.get("keywords") or []
    all_keywords = ", ".join(keywords) if isinstance(keywords, list) else ""
    keywords_ru = data.get("keywords_ru") or keywords
    all_keywords_ru = ", ".join(keywords_ru) if isinstance(keywords_ru, list) else ""

    characteristics = build_characteristics(data)
    # Prom.ua's description field is rich-text/HTML, not plain text - a bare
    # "\n" gets collapsed by normal HTML whitespace rules and renders as no
    # break at all, running the description and characteristics together
    # into one unbroken line. <br> is required for an actual visible break.
    specifications = "".join(f"- {label}: {value};<br>" for label, unit, value in characteristics)
    description = f"{data.get('description', '')}<br><br>Характеристики:<br>{specifications}"
    name = " ".join(part for part in (data.get("name"), data.get("brand")) if part).strip()

    # "Назва_позиції"/"Опис" are Prom.ua's Russian-language fields (the
    # "_укр" suffix marks the Ukrainian counterpart). They must be real
    # Russian text: Prom.ua only auto-translates a Ukrainian field into
    # Russian when the Russian field is left blank, and leaving it blank
    # instead silently drops the whole row on import. Filling it with the
    # Ukrainian text (as a previous version did) satisfies the "non-blank"
    # requirement but permanently defeats the auto-translate, leaving the
    # Russian listing stuck in Ukrainian - so the translation has to be
    # produced here, from the *_ru fields identify_product() fills in.
    specs_ru = [
        ("Бренд", data.get("brand")),
        ("Модель", data.get("model")),
        ("Материал", data.get("material_ru") or data.get("material")),
        ("Цвет", data.get("color_ru") or data.get("color")),
        ("Ширина", data.get("width")),
        ("Высота", data.get("height")),
        ("Длина", data.get("length")),
        ("Вес", data.get("weight")),
    ]
    specifications_ru = "".join(
        f"- {label}: {value};<br>" for label, value in specs_ru if value and value != "null"
    )
    description_ru = (
        f"{data.get('description_ru') or data.get('description', '')}<br><br>"
        f"Характеристики:<br>{specifications_ru}"
    )
    name_ru = " ".join(
        part for part in (data.get("name_ru") or data.get("name"), data.get("brand")) if part
    ).strip()

    return {
        "Ідентифікатор_товару": external_id,
        "Код_товару": article,
        "Назва_позиції": name_ru,
        "Назва_позиції_укр": name,
        "Опис": description_ru,
        "Опис_укр": description,
        "Кількість": 1,
        "Наявність": "available",
        "Ціна": data.get("priceUAH", 0),
        "Посилання_зображення": image_url,
        "Валюта": "UAH",
        "Одиниця_виміру": "шт",
        # data.get(key, "") is a no-op here: the OpenAI schema always
        # includes these keys (just with a null value when unconfirmed), so
        # the dict.get default never actually fires and a null manufacturer
        # was silently reaching Prom.ua as a blank "Виробник" - which
        # Rozetka's feed validation then rejects with a mandatory "vendor"
        # tag error. Falling back to the brand (near-always known, even for
        # unconfirmed products) keeps that field non-blank instead.
        "Виробник": data.get("manufacturer") or data.get("brand") or "",
        "Країна_виробник": data.get("country") or "",
        "Ширина,см": data.get("width") or "",
        "Висота,см": data.get("height") or "",
        "Довжина,см": data.get("length") or "",
        "Вага,кг": data.get("weight") or "",
        "Де_знаходиться_товар": config.PROM_REGION,
        "Пошукові_запити": all_keywords_ru,
        "Пошукові_запити_укр": all_keywords,
        xlsx_builder.CHARACTERISTICS_KEY: characteristics,
    }
