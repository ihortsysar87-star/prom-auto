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

## OLX.ua sync

Separate from the Telegram bot: `olx_sync.py` pushes the existing Prom.ua
catalog to OLX as adverts. It's idempotent (tracks what's already synced in
`.olx_synced.json`) and never spends money - an advert OLX reports "limited"
(free listing quota used up for its category) is left inactive rather than
buying a packet to activate it.

One-time setup, since OLX requires a user-authorized OAuth token (not just a
static API key) to post adverts:

1. Register an app at https://developer.olx.ua/ua/profile/applications and
   set its Redirect URI to a public HTTPS URL you control (OLX rejects
   `localhost`) - a throwaway tunnel like `ngrok http 8765` works.
2. Fill in `OLX_CLIENT_ID`, `OLX_CLIENT_SECRET`, `OLX_CONTACT_NAME`,
   `OLX_CONTACT_PHONE`, `OLX_CITY_ID` (OLX's own numeric city id, not
   Prom.ua's) in `.env`.
3. Run `python -m prom_auto.olx_auth_setup <that same redirect URI>`, open
   the printed URL, log in as the OLX seller account and approve access.
   This saves `OLX_REFRESH_TOKEN` into `.env` - the bot refreshes the access
   token from it automatically afterward, so this only needs to be repeated
   if the refresh token goes unused for 30 days.

Then run the sync:

```
python -m prom_auto.olx_sync            # push everything not yet synced
python -m prom_auto.olx_sync --dry-run   # print payloads without posting
```

## Layout

- `prom_auto/config.py` — env-based settings
- `prom_auto/telegram_bot.py` — Telegram handlers (`/start` mode choice, photo and link flows), orchestrates the full pipeline
- `prom_auto/openai_client.py` — product identification via OpenAI Responses API (photo mode + link-mode page extraction/translation/keywords)
- `prom_auto/product_data_extractor.py` — link-mode pipeline: page fetch, JSON-LD parsing, live currency conversion + automatic 5% discount, image gathering
- `prom_auto/page_fetch.py` — page fetching, with a reader-proxy fallback for bot-protected sites
- `prom_auto/product_mapper.py` — maps identified product data to Prom.ua's import columns
- `prom_auto/xlsx_builder.py` — builds the Prom.ua import xlsx
- `prom_auto/prom_client.py` — Prom.ua API (import_file, list_products)
- `prom_auto/olx_client.py` — OLX API (OAuth token refresh, adverts, categories, cities)
- `prom_auto/olx_mapper.py` — maps a Prom.ua product to OLX's advert payload
- `prom_auto/olx_auth_setup.py` — one-time interactive OLX OAuth login
- `prom_auto/olx_sync.py` — pushes the Prom.ua catalog to OLX as adverts
