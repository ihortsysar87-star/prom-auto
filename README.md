# prom-auto

Python port of the "Igor bot" n8n workflow: a Telegram bot that identifies a
product - from a photo, or from a product page URL - and publishes it to
Prom.ua. Send `/start` to choose: link mode (recommended - faster, no OpenAI
vision/search cost) asks for product URLs one at a time and extracts data
straight from the page, auto-converting the price to UAH and applying a 5%
discount below the source price; photo mode is the original photo → OpenAI
vision + web search flow.

See [migration_plan.html](migration_plan.html) for the full analysis, node-by-node
mapping from the original n8n workflow, and the module breakdown below.

The original workflow's bulk Prom.ua → MySQL sync ("Job B") is out of scope
for now and not ported here.

## Setup

```
pip install -r requirements.txt
cp .env.example .env   # fill in TELEGRAM_TOKEN, OPENAI_API_KEY, PROM_API_TOKEN
```

## Run

```
python -m prom_auto.telegram_bot   # photo -> Prom.ua listing bot
```

## Layout

- `prom_auto/config.py` — env-based settings
- `prom_auto/telegram_bot.py` — Telegram handlers (`/start` mode choice, photo and link flows), orchestrates the full pipeline
- `prom_auto/openai_client.py` — product identification via OpenAI Responses API (photo mode + link-mode page extraction/translation/keywords)
- `prom_auto/product_data_extractor.py` — link-mode pipeline: page fetch, JSON-LD parsing, live currency conversion + automatic 5% discount, image gathering
- `prom_auto/page_fetch.py` — page fetching, with a reader-proxy fallback for bot-protected sites
- `prom_auto/product_mapper.py` — maps identified product data to Prom.ua's import columns
- `prom_auto/xlsx_builder.py` — builds the Prom.ua import xlsx
- `prom_auto/prom_client.py` — Prom.ua API (import_file)
