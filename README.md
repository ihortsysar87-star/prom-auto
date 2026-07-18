# prom-auto

Python port of the "Igor bot" n8n workflow: a Telegram bot that identifies a
product from a photo (via OpenAI) and publishes it to Prom.ua.

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
- `prom_auto/telegram_bot.py` — Telegram photo handler, orchestrates the full pipeline
- `prom_auto/openai_client.py` — product identification via OpenAI Responses API
- `prom_auto/product_mapper.py` — maps OpenAI output to Prom.ua's import columns
- `prom_auto/xlsx_builder.py` — builds the Prom.ua import xlsx
- `prom_auto/prom_client.py` — Prom.ua API (import_file)
