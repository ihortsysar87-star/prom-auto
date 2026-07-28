import base64
import json
import logging

from openai import OpenAI

from . import config

logger = logging.getLogger(__name__)

# Deterministic queries always appended (in addition to whatever the model
# itself proposes) so this coverage doesn't depend on the model remembering
# to think of it - the prompt asking for this was easy for a single agentic
# tool-loop turn to skip under time/token pressure, so it's now guaranteed in
# code instead. Kept to one foreign marketplace (dropship-style comparables)
# and one Ukrainian one (rozetka - the actual local competitor for pricing on
# Prom.ua, and generally well-indexed) rather than four foreign sites, since
# most of those returned little and each guaranteed query costs a full
# web_search call regardless of hit rate.
_GUARANTEED_SITES = ["aliexpress.com", "rozetka.com.ua"]

# Hard cap on how many separate web_search calls one product can trigger
# (model-proposed queries + the guaranteed site ones below) - keeps
# cost/latency bounded while still giving hard-to-identify products a real
# multi-angle search instead of a single shot. In practice the early-stop in
# identify_product() below means most products use far fewer than this.
MAX_SEARCH_QUERIES = 6

# Marker the model is instructed (see _SEARCH_STEP_PROMPT) to write when a
# search query found no exact match for the product - used to detect a real
# confirmed hit in code so identify_product() can stop searching as soon as
# one is found, instead of always burning the full query budget.
_NO_MATCH_MARKER = "немає точного збігу"

_client = OpenAI(api_key=config.OPENAI_API_KEY)

# Structured Outputs (strict JSON Schema) for the two calls that must return
# parseable JSON - without this, a stray unescaped quote in OCR'd packaging
# text (e.g. a `"` for inches, or a quoted phrase) can produce technically-
# invalid JSON that json.loads() rejects outright, silently discarding the
# entire vision read (or the whole synthesized result) and forcing a
# guaranteed "not found" outcome that has nothing to do with search quality.
# strict mode requires every property to be listed in "required" (nullable
# ones just include "null" in their "type") and additionalProperties: false
# on every object level.
_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "visible_text": {"type": ["string", "null"]},
        "barcode": {"type": ["string", "null"]},
        "category": {"type": ["string", "null"]},
        "brand_guess": {"type": ["string", "null"]},
        "model_guess": {"type": ["string", "null"]},
        "attributes": {
            "type": "object",
            "properties": {
                "color": {"type": ["string", "null"]},
                "material": {"type": ["string", "null"]},
                "shape": {"type": ["string", "null"]},
            },
            "required": ["color", "material", "shape"],
            "additionalProperties": False,
        },
        "search_queries": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "visible_text",
        "barcode",
        "category",
        "brand_guess",
        "model_guess",
        "attributes",
        "search_queries",
    ],
    "additionalProperties": False,
}

_DATA_SOURCE_FIELDS = ["name", "brand", "manufacturer", "country", "material", "color"]

_SYNTHESIZE_SCHEMA = {
    "type": "object",
    "properties": {
        # Present (non-null) only for the "couldn't identify at all" case
        # described in rule 5 - every other field is still required (by
        # strict mode) but left null in that case.
        "error": {"type": ["string", "null"]},
        "name": {"type": ["string", "null"]},
        "name_ru": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]},
        "brand": {"type": ["string", "null"]},
        "manufacturer": {"type": ["string", "null"]},
        "country": {"type": ["string", "null"]},
        "material": {"type": ["string", "null"]},
        "material_ru": {"type": ["string", "null"]},
        "color": {"type": ["string", "null"]},
        "color_ru": {"type": ["string", "null"]},
        "width": {"type": ["number", "null"]},
        "height": {"type": ["number", "null"]},
        "length": {"type": ["number", "null"]},
        "weight": {"type": ["number", "null"]},
        "description": {"type": ["string", "null"]},
        "description_ru": {"type": ["string", "null"]},
        "priceUAH": {"type": ["number", "null"]},
        "price_found": {"type": ["boolean", "null"]},
        "price_source_url": {"type": ["string", "null"]},
        "data_sources": {
            "type": "object",
            "properties": {
                field: {"type": "string", "enum": ["photo", "web", "estimated"]}
                for field in _DATA_SOURCE_FIELDS
            },
            "required": _DATA_SOURCE_FIELDS,
            "additionalProperties": False,
        },
        "source_urls": {
            "type": "object",
            "properties": {field: {"type": ["string", "null"]} for field in _DATA_SOURCE_FIELDS},
            "required": _DATA_SOURCE_FIELDS,
            "additionalProperties": False,
        },
        "keywords": {"type": "array", "items": {"type": "string"}},
        "keywords_ru": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "error",
        "name",
        "name_ru",
        "model",
        "brand",
        "manufacturer",
        "country",
        "material",
        "material_ru",
        "color",
        "color_ru",
        "width",
        "height",
        "length",
        "weight",
        "description",
        "description_ru",
        "priceUAH",
        "price_found",
        "price_source_url",
        "data_sources",
        "source_urls",
        "keywords",
        "keywords_ru",
    ],
    "additionalProperties": False,
}

_EXTRACT_PROMPT = """Ти — експерт з розпізнавання товарів за фото. Уважно розглянь усі надані фото ОДНОГО товару (це можуть бути різні ракурси/боки одного й того самого предмета) і виокреми максимум деталей.

Поверни ВИКЛЮЧНО один JSON-об'єкт:
{
  "visible_text": "весь читаний текст з упаковки/товару дослівно, як він написаний",
  "barcode": "цифри штрихкоду EAN/UPC, якщо видно, інакше null",
  "category": "точна категорія товару українською",
  "brand_guess": "найімовірніший бренд, або null якщо визначити неможливо",
  "model_guess": "найімовірніша модель/артикул, або null якщо визначити неможливо",
  "attributes": {"color": "колір", "material": "матеріал", "shape": "форма/конструкція"},
  "search_queries": ["запит 1", "запит 2", "..."]
}

Правила для "search_queries" (масив із 3-5 різних формулювань, від найточнішого до найширшого — ці запити потім РЕАЛЬНО виконає окремий пошуковий інструмент, тому кожен має бути придатним для вставки в пошук як є):
1. Якщо читається штрихкод/артикул/SKU — окремий запит лише з ним, без інших слів (найточніший ідентифікатор).
2. Точна назва/бренд/модель мовою напису на упаковці.
3. Те саме англійською, іншим формулюванням чи порядком слів.
4. Лише модель/артикул без жодних інших слів.
5. Якщо бренд/модель визначити не вдалося (типовий безіменний товар) — ширший запит за категорією і ВСІМА видимими ознаками разом (тип + матеріал + колір + форма + призначення), напр. "силіконова щітка для миття посуду з ручкою рожева" — такі товари так само реально знайти на маркетплейсах за описом, а не за назвою бренду. Це не привід менше шукати, а привід шукати ще уважніше.
"""

_SEARCH_STEP_PROMPT = """Виконай ОДИН пошук інструментом web_search за точно таким запитом: {query!r}

Подивись на результати пошуку. Якщо серед них є реальна сторінка КОНКРЕТНОГО товару (не загальна категорія, не головна сторінка магазину, не інший схожий, а саме той товар, що описаний у запиті) — напиши, що саме там реально написано: точну назву товару, ціну (з валютою), ключові характеристики, мовою сторінки. Якщо точного збігу немає — прямо напиши "Немає точного збігу" і коротко (1 речення), що знайшлося натомість, якщо щось знайшлося.

КРИТИЧНО ВАЖЛИВО: пиши лише те, що реально бачиш у знайдених результатах пошуку. Не вигадуй назви, ціни чи характеристики — якщо чогось не видно в результатах, так і напиши."""

_SYNTHESIZE_PROMPT = """Ти — ШІ-експерт із пошуку товарів. Тобі надано фото товару, розпізнані з фото ознаки, і РЕЗУЛЬТАТИ РЕАЛЬНОГО ПОШУКУ В ІНТЕРНЕТІ (кожен пошук там дійсно виконано окремим викликом web_search — це не твоя пам'ять і не здогадка). Поверни максимально точну інформацію про товар.

ПРАВИЛА:
1. Уважно зістав кожен результат пошуку нижче з тим, що на фото. Результат "підтверджує" товар, лише якщо описує САМЕ цей товар (та сама модель/артикул/дизайн) — а не просто схожу категорію чи інший товар того ж типу.
2. Якщо якийсь результат дійсно підтверджує товар — використай ці дані та постав "web" у "data_sources" для відповідних полів, із ТОЧНО тим URL, що вказаний у розділі "Реально відвідані посилання" для того результату (копіюй дослівно, не змінюй, не скорочуй).
3. КАТЕГОРИЧНО ЗАБОРОНЕНО писати в "source_urls" чи "price_source_url" будь-яку адресу, якої немає дослівно серед "Реально відвіданих посилань" нижче — навіть якщо вона здається правильною або ти "пригадуєш" такий сайт. Такий вигаданий URL миттєво провалює перевірку далі по конвеєру.
4. Якщо жоден результат не підтверджує товар (лише схожі товари, категорія, чи взагалі нічого не знайдено) — не признач "web" для жодного поля; використовуй "photo" (побачив на фото) або "estimated" (обґрунтована оцінка) за правилом 9.
5. Якщо товар взагалі не вдалося ідентифікувати навіть приблизно — поверни "error". Якщо конкретну характеристику не вдалося підтвердити — поверни null для неї (не вигадуй).
6. Локалізація: текстові значення — українською. Ключі JSON — тільки англійською. Додатково заповни "name_ru", "description_ru", "material_ru", "color_ru" — це не буквальний машинний переклад, а природний, грамотний РОСІЙСЬКОЮ мовою варіант відповідних полів (name, description, material, color) для російськомовного покупця на маркетплейсі. Це обов'язкові поля (якщо українське значення null - постав null і тут), Prom.ua НЕ перекладає їх автоматично, тому переклад мусить бути виконаний тут, а не залишений порожнім чи дубльованим з української.
7. Метрика: розміри — в см, вага — в кг. Значення мають бути числами (можна дробові через крапку до двох знаків).
8. Ціна (priceUAH) — другорядне поле. Якщо в підтверджених результатах пошуку (правило 2) трапилась реальна ціна цього товару або дуже схожого — постав "price_found": true, цю ціну (іноземну валюту переведи в грн за поточним курсом) і "price_source_url" — точну URL-адресу з розділу "Реально відвідані посилання". Без такого URL "price_found" не може бути true. Якщо точної ціни немає, але зрозуміла категорія товару — постав "price_found": false, "price_source_url": null і реалістичну орієнтовну ціну на основі цін схожих товарів цієї категорії (не занижуй штучно і не вигадуй довільне число типу 999 без підстави — орієнтуйся на реальний рівень цін такої категорії).
9. Keywords: "keywords" — масив унікальних українських ключових слів для Prom.ua без повторів. "keywords_ru" — той самий набір ключових слів природною РОСІЙСЬКОЮ мовою (не буквальний переклад слово-в-слово, а те, як їх реально шукав би російськомовний покупець), теж без повторів. Обов'язкове поле.
10. Джерело кожного факту: для полів name, brand, manufacturer, country, material, color зазнач у "data_sources", звідки взято значення — "photo" (побачив на самому фото/упаковці), "web" (підтверджено результатом пошуку — правило 2) або "estimated" (не підтверджено ні тим, ні тим). Якщо значення поля null — обов'язково постав йому "estimated" в data_sources (ніколи не "web" і не "photo").
11. Стиль тексту: усі текстові поля (особливо "description") пиши як звичайний опис товару для покупця в інтернет-магазині — БЕЗ згадок про сам процес розпізнавання чи аналізу фото/пошуку. Ніколи не пиши фраз типу "на фото видно", "я бачу, що", "судячи із зображення", "результати пошуку показують" тощо.

СТРУКТУРА ВІДПОВІДІ:
Поверни ВИКЛЮЧНО один JSON-об'єкт без пояснень, Markdown або додаткового тексту.

{
  "name": "Назва товару українською",
  "name_ru": "Название товара русским языком",
  "model": "Модель",
  "brand": "Бренд",
  "manufacturer": "Виробник",
  "country": "Країна виробника",
  "material": "Матеріал",
  "material_ru": "Материал (русским языком)",
  "color": "Колір",
  "color_ru": "Цвет (русским языком)",
  "width": "Ширина",
  "height": "Висота",
  "length": "Довжина",
  "weight": "Вага",
  "description": "Повний опис товару українською мовою",
  "description_ru": "Полное описание товара русским языком",
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
  "keywords": ["ключове слово 1", "ключове слово 2"],
  "keywords_ru": ["ключевое слово 1", "ключевое слово 2"]
}"""


def _image_content(image_bytes_list):
    parts = []
    for image_bytes in image_bytes_list:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        parts.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"})
    return parts


def _extract_features(image_bytes_list) -> dict:
    """Step 1: pure vision read of the photo(s) - no search yet. Produces
    structured attributes plus the model's own candidate search queries,
    which _build_queries() below combines with guaranteed foreign-site
    queries."""
    content = [{"type": "input_text", "text": _EXTRACT_PROMPT}] + _image_content(image_bytes_list)
    response = _client.responses.create(
        model=config.OPENAI_MODEL,
        input=[{"role": "user", "content": content}],
        max_output_tokens=1500,
        text={"format": {"type": "json_schema", "name": "product_features", "schema": _EXTRACT_SCHEMA, "strict": True}},
    )
    return extract_json(response)


def _best_query_text(features: dict) -> str:
    barcode = (features.get("barcode") or "").strip()
    if barcode:
        return barcode
    brand = (features.get("brand_guess") or "").strip()
    model = (features.get("model_guess") or "").strip()
    if brand or model:
        return " ".join(p for p in (brand, model) if p)
    visible = (features.get("visible_text") or "").strip()
    if visible:
        return visible
    return (features.get("category") or "").strip()


def _build_queries(features: dict) -> list[str]:
    """Model-proposed queries (up to 4, ordered specific->broad per
    _EXTRACT_PROMPT) plus one guaranteed query per site in
    _GUARANTEED_SITES, deduplicated and capped at MAX_SEARCH_QUERIES - the
    guaranteed-site coverage doesn't depend on the model thinking of it."""
    queries = [q.strip() for q in (features.get("search_queries") or []) if q and q.strip()][:4]

    base_text = _best_query_text(features)
    if base_text:
        for site in _GUARANTEED_SITES:
            queries.append(f"site:{site} {base_text}")

    seen = set()
    deduped = []
    for q in queries:
        key = q.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(q)
    return deduped[:MAX_SEARCH_QUERIES]


def _run_search_query(query: str) -> dict:
    """Step 2 (one call per query): forces exactly one real web_search
    invocation and asks the model to honestly report what it actually saw,
    rather than letting a single hidden multi-round tool loop decide on its
    own when it's done searching."""
    response = _client.responses.create(
        model=config.OPENAI_MODEL,
        tools=[{"type": "web_search"}],
        tool_choice="required",
        input=[{"role": "user", "content": [{"type": "input_text", "text": _SEARCH_STEP_PROMPT.format(query=query)}]}],
        max_output_tokens=1200,
    )

    report_lines = []
    cited_urls: set[str] = set()
    for item in response.output:
        if item.type == "message":
            for content_part in item.content:
                text = getattr(content_part, "text", None)
                if text:
                    report_lines.append(text)
                for annotation in getattr(content_part, "annotations", None) or []:
                    if getattr(annotation, "type", None) == "url_citation":
                        cited_urls.add(annotation.url)

    return {"query": query, "report": "\n".join(report_lines).strip(), "cited_urls": cited_urls}


def _format_evidence(features: dict, evidence: list[dict]) -> str:
    lines = ["РОЗПІЗНАНІ ОЗНАКИ З ФОТО:", json.dumps(features, ensure_ascii=False, indent=2), ""]

    if evidence:
        lines.append("РЕЗУЛЬТАТИ РЕАЛЬНОГО ПОШУКУ В ІНТЕРНЕТІ (кожен запит дійсно виконано інструментом web_search):")
        for i, e in enumerate(evidence, 1):
            lines.append(f"\n--- Пошук {i}: запит = {e['query']!r} ---")
            lines.append(e["report"] or "(модель не повернула текстового звіту для цього запиту)")
            if e["cited_urls"]:
                lines.append("Реально відвідані посилання: " + ", ".join(sorted(e["cited_urls"])))
            else:
                lines.append("Реально відвіданих посилань немає для цього запиту.")
    else:
        lines.append("Пошук в інтернеті не виконувався (не вдалося сформувати запити) — покладайся лише на фото.")

    return "\n".join(lines)


def identify_product(image_bytes_list):
    """Equivalent of n8n nodes 'Parse raw data2' + 'HTTP Request api.openai',
    but restructured into three explicit, code-driven steps instead of one
    opaque model turn that reads the photo, formulates queries, searches,
    and decides when to stop, all in a single hidden agentic loop:

      1. _extract_features - vision-only read of the photo(s), no search.
      2. _build_queries + _run_search_query - code runs each candidate
         query (the model's own proposals plus guaranteed foreign-site
         queries) as its own separate, forced web_search call, so real
         search coverage doesn't depend on the model faithfully executing a
         multi-step protocol described in a prompt.
      3. Final synthesis call - the model gets the photo plus all the real
         search evidence gathered above and is told it may only cite URLs
         that literally appear in that evidence, removing its ability to
         "decide" a weak first search was good enough or to hallucinate a
         plausible-looking source.

    Returns (response, search_count, cited_urls) where search_count is the
    number of real web_search steps actually run, and cited_urls is the
    union of URLs the tool actually retrieved across all of them -
    telegram_bot.py cross-checks source_urls against this set so a field can
    only be trusted as "web" if some search really visited that page.
    """
    features: dict = {}
    try:
        features = _extract_features(image_bytes_list)
    except Exception:
        logger.exception("Feature extraction step failed, continuing with empty features")

    queries = _build_queries(features)
    logger.info("Search queries for this product (%d): %r", len(queries), queries)

    evidence = []
    all_cited_urls: set[str] = set()
    for query in queries:
        try:
            result = _run_search_query(query)
        except Exception:
            logger.exception("Search step failed for query %r, continuing with the rest", query)
            continue
        logger.info("Search step: query=%r -> %d citation(s)", query, len(result["cited_urls"]))
        evidence.append(result)
        all_cited_urls.update(result["cited_urls"])

        # Early stop: once a query reports a real confirmed page (some URL
        # was actually retrieved and the model didn't flag it as "no exact
        # match"), further queries are very unlikely to be worth their
        # cost - the synthesis step below independently re-verifies the
        # match anyway, so a false-positive stop here just falls back to
        # "estimated" same as if we'd never found it. This restores the
        # "stop once you have a confirmed hit" behavior the single-prompt
        # version relied on, but enforced in code instead of hoping the
        # model's own agentic loop honors it.
        if result["cited_urls"] and _NO_MATCH_MARKER not in result["report"].lower():
            logger.info("Query %r looks confirmed - stopping further searches early", query)
            break

    synth_content = (
        [{"type": "input_text", "text": _SYNTHESIZE_PROMPT}]
        + [{"type": "input_text", "text": _format_evidence(features, evidence)}]
        + _image_content(image_bytes_list)
    )
    response = _client.responses.create(
        model=config.OPENAI_MODEL,
        input=[{"role": "user", "content": synth_content}],
        max_output_tokens=3000,
        text={
            "format": {
                "type": "json_schema",
                "name": "product_identification",
                "schema": _SYNTHESIZE_SCHEMA,
                "strict": True,
            }
        },
    )

    search_count = len(evidence)
    logger.info("Total search steps run: %d, %d distinct cited URL(s)", search_count, len(all_cited_urls))

    return response, search_count, all_cited_urls


def extract_json(response):
    """Pulls the JSON payload out of the last output item's text, like the
    `JSON.parse(text)` calls scattered across the n8n Code nodes."""
    last_output = response.output[-1]
    text = last_output.content[0].text
    return json.loads(text)
