import base64

from openai import OpenAI

from . import config

_PROMPT = """Ти — ШІ-експерт із пошуку товарів. За назвою чи фото знайди товар і поверни максимально точну інформацію.

ПРАВИЛА ПОШУКУ:
1. Джерела: Шукай насамперед на українських сайтах, далі — на міжнародних. Використовуй різні підходи. Бери найточніші дані. Якщо товар не знайдено — поверни "error". Якщо характеристика не підтверджена — поверни null.
2. Локалізація: Текстові значення — українською. Ключі JSON — тільки англійською.
3. Метрика: Розміри — в см, вага — в кг. Значення мають бути числами (можна дробові через крапку до двох знаків).
4. Ціна (priceUAH): Тільки число без символів валюти. Іноземну валюту переведи в грн. Вибери середню актуальну ціну. Якщо ціну не знайдено — постав 999.
5. Keywords: Масив унікальних українських ключових слів для Prom.ua без повторів.

СТРУКТУРА ВІДПОВІДІ:
Поверни ВИКЛЮЧНО один JSON-об'єкт без пояснень, Markdown або додаткового тексту.

{
  "name": "Назва товару українською",
  "model": "Модель",
  "brand": "Бренд",
  "manufacturer": "Виробник",
  "country": "Країна виробника",
  "material": "Матеріал",
  "color": "Колір",
  "width": "Ширина",
  "height": "Висота",
  "length": "Довжина",
  "weight": "Вага",
  "description": "Повний опис товару українською мовою",
  "priceUAH": 1499,
  "keywords": ["ключове слово 1", "ключове слово 2"]
}"""

_client = OpenAI(api_key=config.OPENAI_API_KEY)


def identify_product(image_bytes_list):
    """Equivalent of n8n nodes 'Parse raw data2' + 'HTTP Request api.openai'.

    Sends one or more product photos to the OpenAI Responses API with web
    search enabled and returns the raw response object.
    """
    content = [{"type": "input_text", "text": _PROMPT}]
    for image_bytes in image_bytes_list:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        content.append(
            {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"}
        )

    return _client.responses.create(
        model=config.OPENAI_MODEL,
        tools=[
            {
                "type": "web_search",
                "search_content_types": ["image", "text"],
                "image_settings": {"max_results": 3, "caption": True},
            }
        ],
        include=["web_search_call.results"],
        input=[{"role": "user", "content": content}],
        max_output_tokens=2000,
    )


def extract_json(response):
    """Pulls the JSON payload out of the last output item's text, like the
    `JSON.parse(text)` calls scattered across the n8n Code nodes."""
    import json

    last_output = response.output[-1]
    text = last_output.content[0].text
    return json.loads(text)


def extract_image_url(response):
    """Returns the first real product photo found by the web_search tool's
    image results, or None if the search surfaced nothing usable."""
    for item in response.output:
        if item.type != "web_search_call":
            continue
        for result in getattr(item, "results", None) or []:
            if getattr(result, "type", None) == "image_result":
                return result.image_url
    return None
