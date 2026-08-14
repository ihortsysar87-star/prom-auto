"""One-time interactive login to obtain an OLX refresh_token.

Posting adverts via OLX's API requires the OAuth2 `authorization_code` grant
(the "user context" flow) - a static client_id/client_secret alone only
covers read-only, non-user actions (categories, cities, etc.), not creating
adverts. This script drives that one-time login:

1. Prints an OLX login/consent URL for you to open in a browser.
2. You log in as the OLX seller account and approve access.
3. OLX redirects your browser to --redirect-uri (which must already be
   registered on this app in OLX's App Manager, and must be a public URL -
   OLX rejects localhost. A throwaway tunnel, e.g.
   `ngrok http 8765`, pointed at this script's local port works fine.)
4. This script's local server (bound to the same port the tunnel forwards
   to) catches that redirect, exchanges the code for an access_token +
   refresh_token, and writes OLX_REFRESH_TOKEN into .env.

Usage:
    python -m prom_auto.olx_auth_setup https://<your-tunnel-host>/oauth/callback
"""
from __future__ import annotations

import secrets
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from . import config, olx_client

_CALLBACK_PORT = 8765
_CALLBACK_TIMEOUT_SECONDS = 300


def _build_authorize_url(redirect_uri: str, state: str) -> str:
    params = {
        "client_id": config.OLX_CLIENT_ID,
        "response_type": "code",
        "state": state,
        "scope": "read write v2",
        "redirect_uri": redirect_uri,
    }
    return f"{config.OLX_AUTHORIZE_URL}/?{urllib.parse.urlencode(params)}"


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self) -> None:  # noqa: N802 (http.server's required method name)
        parsed = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.result = {k: v[0] for k, v in params.items()}

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if "code" in params:
            self.wfile.write("<html><body>OLX authorized - you can close this tab.</body></html>".encode())
        else:
            self.wfile.write("<html><body>No authorization code received.</body></html>".encode())

    def log_message(self, fmt: str, *args) -> None:
        pass  # silence BaseHTTPRequestHandler's default request logging


def main() -> None:
    if not config.OLX_CLIENT_ID or not config.OLX_CLIENT_SECRET:
        print("Set OLX_CLIENT_ID and OLX_CLIENT_SECRET in .env before running this.")
        sys.exit(1)
    if len(sys.argv) != 2:
        print(f"Usage: python -m prom_auto.olx_auth_setup <redirect_uri registered in OLX App Manager>")
        sys.exit(1)
    redirect_uri = sys.argv[1]

    state = secrets.token_urlsafe(16)
    authorize_url = _build_authorize_url(redirect_uri, state)

    print("1. Open this URL, log in as the OLX seller account, and approve access:\n")
    print(f"   {authorize_url}\n")
    print(f"2. Waiting for the redirect back to {redirect_uri} ...")

    server = HTTPServer(("localhost", _CALLBACK_PORT), _CallbackHandler)
    server.timeout = _CALLBACK_TIMEOUT_SECONDS
    server.handle_request()

    result = _CallbackHandler.result
    if not result.get("code"):
        print(f"No authorization code received within {_CALLBACK_TIMEOUT_SECONDS}s. Error: {result}")
        sys.exit(1)
    if result.get("state") != state:
        print("state parameter mismatch - the callback didn't come from the request this script made. Aborting.")
        sys.exit(1)

    print("3. Got the authorization code, exchanging it for tokens...")
    try:
        tokens = olx_client.exchange_code_for_tokens(result["code"], redirect_uri)
    except olx_client.OlxAuthError as exc:
        print(f"Token exchange failed: {exc}")
        sys.exit(1)

    olx_client._persist_refresh_token(tokens["refresh_token"])
    # Also seed the in-memory access token cache so a get_access_token() call
    # right after this script exits (e.g. a quick manual sanity check) doesn't
    # need an extra refresh round-trip.
    olx_client._access_token = tokens["access_token"]
    olx_client._access_token_expires_at = time.monotonic() + tokens.get("expires_in", 0)

    me = requests.get(
        f"{config.OLX_API_BASE_URL}/users/me",
        headers={"Authorization": f"Bearer {tokens['access_token']}", "Version": "2.0"},
        timeout=15,
    )
    who = me.json().get("name", "?") if me.ok else "(couldn't confirm - see below)"

    print(f"\nDone. OLX_REFRESH_TOKEN saved to .env. Logged in as: {who}")
    print("The bot will refresh the access token automatically from now on - no need to run this again")
    print("unless the refresh token is left unused for 30 days.")


if __name__ == "__main__":
    main()
