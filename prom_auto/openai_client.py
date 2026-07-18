import base64
import logging

from openai import OpenAI

from . import config

logger = logging.getLogger(__name__)

_PROMPT = """Ти — ШІ-експерт із пошуку товарів. За фото знайди товар і поверни максимально точну інформацію.

ПРАВИЛА ПОШУКУ:
1. Спочатку уважно опиши собі, що саме на фото: видимий текст, логотипи, форма, матеріал, призначення — це твої пошукові підказки.
2. Якщо надано "ПІДКАЗКИ ВІД ПОШУКУ ЗОБРАЖЕННЯ" — це реальні сторінки з таким самим або дуже схожим фото, знайдені окремим сервісом розпізнавання зображень. Обов'язково перевір ці сторінки пошуком в першу чергу — це найточніше джерело, набагато надійніше за просто текстовий запит.
3. Додатково зроби МІНІМУМ 2-3 різні пошукові запити з різним формулюванням (за брендом+моделлю, за категорією+ознаками, українською і англійською), перш ніж робити висновок. Одного запиту недостатньо.
5. Джерела: перевір насамперед популярні українські майданчики — rozetka.com.ua, prom.ua, epicentrk.ua, hotline.ua, olx.ua — потім міжнародні сайти. Бери найточніші та найсвіжіші дані.
6. Якщо товар взагалі не вдалося ідентифікувати навіть приблизно — поверни "error". Якщо конкретну характеристику не вдалося підтвердити — поверни null для неї (не вигадуй).
7. Локалізація: текстові значення — українською. Ключі JSON — тільки англійською.
8. Метрика: розміри — в см, вага — в кг. Значення мають бути числами (можна дробові через крапку до двох знаків).
9. Ціна (priceUAH): тільки число без символів валюти. Іноземну валюту переведи в грн за поточним курсом. Якщо знайшов ціну цього товару або дуже схожого — постав "price_found": true і реальну ціну. Якщо точної ціни немає, але ти впевнений у категорії товару — постав "price_found": false і орієнтовну ціну для подібних товарів цієї категорії (не вигадуй довільне число, спирайся на схожі товари з пошуку). Ніколи не став фіксовану заглушку типу 999 без підстави.
10. Keywords: масив унікальних українських ключових слів для Prom.ua без повторів.
11. Джерело кожного факту: для полів name, brand, manufacturer, country, material, color зазнач у "data_sources", звідки взято значення — "photo" (побачив на самому фото/упаковці — напис, логотип, візуальна ознака), "web" (підтверджено сторінкою конкретного товару чи виробника в пошуку) або "estimated" (не підтверджено ні тим, ні тим — твоє припущення). Не позначай "web", якщо насправді просто прочитав текст на упаковці.

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
  "price_found": true,
  "data_sources": {
    "name": "photo",
    "brand": "photo",
    "manufacturer": "web",
    "country": "web",
    "material": "photo",
    "color": "photo"
  },
  "keywords": ["ключове слово 1", "ключове слово 2"]
}"""

_client = OpenAI(api_key=config.OPENAI_API_KEY)


def _format_image_search_hints(hints: dict) -> str:
    """Turns Google Vision Web Detection results into a text block the
    model can use as real search leads instead of guessing from the photo
    alone."""
    if not hints:
        return ""

    lines = []
    if hints.get("guess_labels"):
        lines.append("Ймовірна назва товару: " + ", ".join(hints["guess_labels"]))
    if hints.get("entity_names"):
        lines.append("Пов'язані поняття: " + ", ".join(hints["entity_names"]))
    if hints.get("page_urls"):
        lines.append(
            "Сторінки з таким самим або дуже схожим зображенням (перевір їх пошуком):\n"
            + "\n".join(hints["page_urls"])
        )

    if not lines:
        return ""
    return "ПІДКАЗКИ ВІД ПОШУКУ ЗОБРАЖЕННЯ:\n" + "\n".join(lines)


def identify_product(image_bytes_list, image_search_hints=None):
    """Equivalent of n8n nodes 'Parse raw data2' + 'HTTP Request api.openai'.

    Sends one or more product photos to the OpenAI Responses API with web
    search enabled and returns the raw response object. image_search_hints,
    if given, comes from reverse_image_search.find_web_matches() and is
    injected as real search leads.
    """
    content = [{"type": "input_text", "text": _PROMPT}]
    hint_text = _format_image_search_hints(image_search_hints)
    if hint_text:
        content.append({"type": "input_text", "text": hint_text})
    for image_bytes in image_bytes_list:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        content.append(
            {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"}
        )

    response = _client.responses.create(
        model=config.OPENAI_MODEL,
        tools=[{"type": "web_search"}],
        tool_choice="required",
        input=[{"role": "user", "content": content}],
        max_output_tokens=2000,
    )

    for item in response.output:
        if item.type == "web_search_call":
            action = getattr(item, "action", None)
            query = getattr(action, "query", None) if action else None
            logger.info("web_search query used: %r", query)

    return response


def extract_json(response):
    """Pulls the JSON payload out of the last output item's text, like the
    `JSON.parse(text)` calls scattered across the n8n Code nodes."""
    import json

    last_output = response.output[-1]
    text = last_output.content[0].text
    return json.loads(text)
