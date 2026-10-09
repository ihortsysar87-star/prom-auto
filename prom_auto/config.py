import os

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.4")

PROM_API_TOKEN = os.environ.get("PROM_API_TOKEN", "")
PROM_API_BASE_URL = "https://my.prom.ua/api/v1"
PROM_REGION = os.environ.get("PROM_REGION", "Київ")

IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY", "")

# Chat ID of the separate Telegram group products also get posted to, with a
# manually-set price (unlike Prom.ua, which gets an AI-estimated price).
# Negative for groups/supergroups. Get it by adding the bot to the group as
# admin and sending any message there - the bot logs every chat ID it sees.
SALES_GROUP_CHAT_ID = os.environ.get("SALES_GROUP_CHAT_ID", "")
MANAGER_CONTACT_URL = os.environ.get("MANAGER_CONTACT_URL", "")

# OLX.ua API (OAuth2 - see olx_auth_setup.py for the one-time login that
# produces OLX_REFRESH_TOKEN). client_id/client_secret come from OLX's App
# Manager (https://developer.olx.ua/ua/profile/applications); posting adverts
# requires a user-context refresh_token, not just the static app credentials.
OLX_CLIENT_ID = os.environ.get("OLX_CLIENT_ID", "")
OLX_CLIENT_SECRET = os.environ.get("OLX_CLIENT_SECRET", "")
OLX_REFRESH_TOKEN = os.environ.get("OLX_REFRESH_TOKEN", "")
OLX_API_BASE_URL = "https://www.olx.ua/api/partner"
OLX_TOKEN_URL = "https://www.olx.ua/api/open/oauth/token"
OLX_AUTHORIZE_URL = "https://www.olx.ua/oauth/authorize"
OLX_ADVERTISER_TYPE = os.environ.get("OLX_ADVERTISER_TYPE", "private")
OLX_CONTACT_NAME = os.environ.get("OLX_CONTACT_NAME", "")
OLX_CONTACT_PHONE = os.environ.get("OLX_CONTACT_PHONE", "")
OLX_CITY_ID = os.environ.get("OLX_CITY_ID", "")
OLX_DISTRICT_ID = os.environ.get("OLX_DISTRICT_ID", "")
OLX_CONDITION = os.environ.get("OLX_CONDITION", "new")
# Which .env variable OLX_REFRESH_TOKEN lives in (olx_client rewrites it when
# OLX rotates the token) and which account's state file olx_sync uses.
OLX_REFRESH_TOKEN_ENV = "OLX_REFRESH_TOKEN"
OLX_ACCOUNT = ""


def use_olx_account(account: str) -> None:
    """Switches the OLX_* settings above to a secondary OLX seller account.

    Account "2" reads OLX2_CLIENT_ID, OLX2_REFRESH_TOKEN, OLX2_CONTACT_NAME,
    etc. from .env; any OLX2_* variable that isn't set falls back to the
    primary account's OLX_* value (so e.g. the same OLX app credentials or
    city can be shared). An empty account keeps the primary one. Call before
    any olx_client/olx_mapper use - one process works with one account."""
    global OLX_CLIENT_ID, OLX_CLIENT_SECRET, OLX_REFRESH_TOKEN, OLX_ADVERTISER_TYPE
    global OLX_CONTACT_NAME, OLX_CONTACT_PHONE, OLX_CITY_ID, OLX_DISTRICT_ID, OLX_CONDITION
    global OLX_REFRESH_TOKEN_ENV, OLX_ACCOUNT
    if not account:
        return
    prefix = f"OLX{account}_"

    def pick(name: str, primary: str) -> str:
        return os.environ.get(prefix + name, primary)

    OLX_CLIENT_ID = pick("CLIENT_ID", OLX_CLIENT_ID)
    OLX_CLIENT_SECRET = pick("CLIENT_SECRET", OLX_CLIENT_SECRET)
    # Never inherit the primary refresh token - that would act as the wrong user.
    OLX_REFRESH_TOKEN = os.environ.get(prefix + "REFRESH_TOKEN", "")
    OLX_ADVERTISER_TYPE = pick("ADVERTISER_TYPE", OLX_ADVERTISER_TYPE)
    OLX_CONTACT_NAME = pick("CONTACT_NAME", OLX_CONTACT_NAME)
    OLX_CONTACT_PHONE = pick("CONTACT_PHONE", OLX_CONTACT_PHONE)
    OLX_CITY_ID = pick("CITY_ID", OLX_CITY_ID)
    OLX_DISTRICT_ID = pick("DISTRICT_ID", OLX_DISTRICT_ID)
    OLX_CONDITION = pick("CONDITION", OLX_CONDITION)
    OLX_REFRESH_TOKEN_ENV = prefix + "REFRESH_TOKEN"
    OLX_ACCOUNT = account


# `OLX_ACCOUNT=2 python -m prom_auto.olx_sync` is equivalent to `--account 2`.
use_olx_account(os.environ.get("OLX_ACCOUNT", ""))

# Rozetka's own feed-validation report (seller.rozetka.com.ua) - not a Prom.ua
# or Rozetka API, just the hash tied to your account's validation page, used
# by rozetka_backfill.py to find products it flagged as missing vendor/
# characteristics.
ROZETKA_VALIDATE_HASH = os.environ.get("ROZETKA_VALIDATE_HASH", "")

# Rozetka Marketplace's own direct seller API (api-seller.rozetka.com.ua) -
# a completely separate account/credentials from PROM_API_TOKEN above. Only
# needed for rozetka_client.py, which can fix vendor/characteristics
# directly on Rozetka instead of going through a Prom.ua re-import.
# ROZETKA_SELLER_ACCESS_TOKEN is a token generated directly in the seller
# panel; if set, rozetka_client.py uses it as-is and skips the
# username/password login below entirely.
ROZETKA_SELLER_ACCESS_TOKEN = os.environ.get("ROZETKA_SELLER_ACCESS_TOKEN", "")
ROZETKA_SELLER_USERNAME = os.environ.get("ROZETKA_SELLER_USERNAME", "")
ROZETKA_SELLER_PASSWORD = os.environ.get("ROZETKA_SELLER_PASSWORD", "")
ROZETKA_SELLER_API_BASE_URL = "https://api-seller.rozetka.com.ua"
