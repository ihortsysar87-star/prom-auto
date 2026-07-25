import base64
import logging

from openai import OpenAI

from . import config

logger = logging.getLogger(__name__)

_PROMPT = """Ти — ШІ-експерт із пошуку товарів. За фото знайди товар і поверни максимально точну інформацію.

ПРАВИЛА ПОШУКУ:
1. Спочатку уважно опиши собі, що саме на фото: видимий текст, логотипи, форма, матеріал, призначення — це твої пошукові підказки.
2. Виконай пошук інструментом web_search на основі того, що побачив на фото. Якщо перший запит одразу дав точну сторінку саме цього товару (з якої можна взяти справжній URL) — цього достатньо, далі не шукай. АЛЕ якщо перший запит НЕ дав точного збігу — ЦЕ ЩЕ НЕ ПРИВІД ЗУПИНЯТИСЯ: одна невдала спроба нічого не доводить, спробуй ще щонайменше 3 РІЗНІ формулювання, перш ніж визнати товар непідтвердженим:
   - запит 1: точна назва/бренд/модель, яку ти побачив на фото (мовою напису на упаковці).
   - запит 2: те саме англійською (навіть якщо напис уже англійською — спробуй інше формулювання або порядок слів).
   - запит 3: ЛИШЕ модель/артикул/штрихкод/номер без жодних інших слів — зайві уточнювальні слова в запиті 1-2 могли просто не збігтися з текстом на реальній сторінці товару.
   - запит 4: за категорією товару та ключовими ознаками (матеріал, розмір, призначення) — ширший запит, якщо точну модель так і не вдалося знайти.
   - Лише після ЩОНАЙМЕНШЕ 4 різних запитів (або раніше, якщо один із них уже дав точний підтверджений збіг) можна визнати товар непідтвердженим і поставити "estimated"/"photo" за правилом 9 — не вигадуй підтвердження.
3. Джерела: не обмежуйся лише кількома сайтами — шукай на будь-яких інтернет-магазинах, маркетплейсах, сайтах виробників чи офіційних дистриб'юторів, які реально продають саме цей товар. Українські майданчики (rozetka.com.ua, prom.ua, epicentrk.ua, hotline.ua, olx.ua та інші) — пріоритет, перевіряй їх першими. Але якщо там точного збігу немає — обов'язково пробуй й інші сайти (закордонні магазини, aliexpress, amazon, ebay, офіційний сайт бренду тощо) — мета знайти РЕАЛЬНУ сторінку саме цього товару, а не зупинитись на кількох майданчиках. З кількох знайдених відповідних сторінок обирай ту, де є якісні фото товару (кілька фото, хороша роздільна здатність) — ці фото потім використовуються замість фото користувача, тож якість джерела має значення. Мета — знайти точний збіг завжди, коли товар реально продається десь в інтернеті; не здавайся передчасно.
4. Якщо товар взагалі не вдалося ідентифікувати навіть приблизно — поверни "error". Якщо конкретну характеристику не вдалося підтвердити — поверни null для неї (не вигадуй).
5. Локалізація: текстові значення — українською. Ключі JSON — тільки англійською.
6. Метрика: розміри — в см, вага — в кг. Значення мають бути числами (можна дробові через крапку до двох знаків).
7. Ціна (priceUAH) — другорядне поле. Якщо серед підтверджених даних (правило 2) трапилась реальна ціна цього товару або дуже схожого — постав "price_found": true, цю ціну (іноземну валюту переведи в грн за поточним курсом) і "price_source_url" — точну URL-адресу сторінки з цією ціною. Без конкретного URL "price_found" не може бути true. Якщо точної ціни немає, але зрозуміла категорія товару — постав "price_found": false, "price_source_url": null і реалістичну орієнтовну ціну на основі цін схожих товарів цієї категорії (не занижуй штучно і не вигадуй довільне число типу 999 без підстави — орієнтуйся на реальний рівень цін такої категорії).
8. Keywords: масив унікальних українських ключових слів для Prom.ua без повторів.
9. Джерело кожного факту: для полів name, brand, manufacturer, country, material, color зазнач у "data_sources", звідки взято значення — "photo" (побачив на самому фото/упаковці — напис, логотип, візуальна ознака), "web" (підтверджено сторінкою конкретного товару чи виробника — з правила 2) або "estimated" (не підтверджено ні тим, ні тим — твоє припущення). Не позначай "web", якщо насправді просто прочитав текст на упаковці. Якщо значення поля null — обов'язково постав йому і "estimated" в data_sources (ніколи не "web" і не "photo": null не може бути "підтверджений" чи "побачений").
10. Якщо в "data_sources" якесь поле позначено "web" — ОБОВ'ЯЗКОВО додай для нього справжню URL-адресу сторінки, де саме це підтверджено, в "source_urls" (той самий ключ поля). Це реальна перевірювана адреса з результатів пошуку правила 2, не вигадана. Якщо не можеш навести конкретний URL для поля — не став йому "web", став "estimated" замість цього.
11. Стиль тексту: усі текстові поля (особливо "description") пиши як звичайний опис товару для покупця в інтернет-магазині — БЕЗ згадок про сам процес розпізнавання чи аналізу фото. Ніколи не пиши фраз типу "на фото видно", "я бачу, що", "судячи із зображення", "схоже, що це" тощо — читач опису не має знати, що дані взято з фото чи пошуку.

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
  "price_source_url": "https://приклад-справжньої-сторінки-з-ціною",
  "data_sources": {
    "name": "photo",
    "brand": "photo",
    "manufacturer": "web",
    "country": "web",
    "material": "photo",
    "color": "photo"
  },
  "source_urls": {
    "manufacturer": "https://приклад-справжньої-сторінки-виробника",
    "country": "https://приклад-справжньої-сторінки-виробника"
  },
  "keywords": ["ключове слово 1", "ключове слово 2"]
}"""

_client = OpenAI(api_key=config.OPENAI_API_KEY)


def identify_product(image_bytes_list):
    """Equivalent of n8n nodes 'Parse raw data2' + 'HTTP Request api.openai'.

    Sends one or more product photos to the OpenAI Responses API with web
    search enabled. The model reads the photo itself (brand/model/text/logo)
    and confirms via web_search - trust in "web"-sourced fields comes from
    the source_urls/price_source_url check in telegram_bot.py, not from how
    many searches were run.

    Returns (response, search_count) where search_count is the number of
    actual web_search_call tool invocations, kept for logging/observability.
    """
    content = [{"type": "input_text", "text": _PROMPT}]
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

    search_count = 0
    for item in response.output:
        if item.type == "web_search_call":
            search_count += 1
            action = getattr(item, "action", None)
            query = getattr(action, "query", None) if action else None
            logger.info("web_search query used: %r", query)
        elif item.type == "message":
            for content_part in item.content:
                for annotation in getattr(content_part, "annotations", None) or []:
                    if getattr(annotation, "type", None) == "url_citation":
                        logger.info(
                            "web_search result read by model: %s (%r)",
                            annotation.url,
                            getattr(annotation, "title", None),
                        )
    logger.info("Total web_search calls: %d", search_count)

    return response, search_count


def extract_json(response):
    """Pulls the JSON payload out of the last output item's text, like the
    `JSON.parse(text)` calls scattered across the n8n Code nodes."""
    import json

    last_output = response.output[-1]
    text = last_output.content[0].text
    return json.loads(text)
