"""
Salesforce REST API client — PKCE OAuth flow.

Auth flow:
  1. Run callback_server.py in a separate terminal  (python callback_server.py)
  2. Open http://localhost:8001/api/auth/login in your browser
  3. Log in to Salesforce — it redirects to localhost:8080/callback
  4. callback_server.py exchanges the code, saves auth_data.json, then exits
  5. All subsequent API calls auto-refresh from auth_data.json — no re-login needed

.env only needs SF_CLIENT_ID, SF_CLIENT_SECRET (optional), and SF_INSTANCE_URL.
"""

import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config from .env ──────────────────────────────────────────────────────────
SF_CLIENT_ID     = os.getenv("SF_CLIENT_ID", "")
SF_CLIENT_SECRET = os.getenv("SF_CLIENT_SECRET", "")   # optional for PKCE
SF_INSTANCE_URL  = os.getenv("SF_INSTANCE_URL", "")

QUERY_PATH   = "/services/data/v59.0/query"
REDIRECT_URI = "http://localhost:8080/callback"
SCOPES       = "api refresh_token offline_access"

TOKEN_EXPIRY_BUFFER = 300  # refresh 5 min before expiry

# Token/PKCE files sit next to this file
_BASE      = Path(__file__).resolve().parent
TOKEN_FILE = str(_BASE / "auth_data.json")
PKCE_FILE  = str(_BASE / "pkce_state.json")


class SalesforceError(Exception):
    pass


# ── PKCE helpers ──────────────────────────────────────────────────────────────

def _pkce_pair():
    verifier  = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge

def _random_state():
    return base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode()

def _save_pkce(data: dict):
    Path(PKCE_FILE).write_text(json.dumps(data))

def _load_pkce() -> dict | None:
    try:
        return json.loads(Path(PKCE_FILE).read_text())
    except Exception:
        return None

def _clear_pkce():
    try:
        Path(PKCE_FILE).unlink(missing_ok=True)
    except Exception:
        pass


# ── Token storage ─────────────────────────────────────────────────────────────

def _save_token(refresh_token: str, instance_url: str,
                access_token: str = "", expires_in: int = 3600):
    Path(TOKEN_FILE).write_text(json.dumps({
        "refresh_token": refresh_token,
        "instance_url":  instance_url,
        "access_token":  access_token,
        "expires_at":    time.time() + expires_in - TOKEN_EXPIRY_BUFFER,
    }))

def _load_token() -> dict | None:
    try:
        data = json.loads(Path(TOKEN_FILE).read_text())
        if data.get("refresh_token") and data.get("instance_url"):
            return data
    except Exception:
        pass
    return None

def _token_needs_refresh(data: dict) -> bool:
    return time.time() >= data.get("expires_at", 0)

def clear_token():
    """Log out — delete stored tokens."""
    try:
        Path(TOKEN_FILE).unlink(missing_ok=True)
    except Exception:
        pass
    _clear_pkce()

def is_authenticated() -> bool:
    """Return True if a stored token exists (doesn't verify it's still valid)."""
    return _load_token() is not None


# ── OAuth login / callback ────────────────────────────────────────────────────

def build_login_url() -> str:
    """
    Generate a Salesforce PKCE login URL and save PKCE state to disk.
    Open this URL in a browser to begin the OAuth flow.
    """
    verifier, challenge = _pkce_pair()
    state = _random_state()
    _save_pkce({
        "verifier":      verifier,
        "state":         state,
        "client_id":     SF_CLIENT_ID,
        "client_secret": SF_CLIENT_SECRET,
        "instance_url":  SF_INSTANCE_URL,
    })
    return (
        f"{SF_INSTANCE_URL}/services/oauth2/authorize"
        f"?response_type=code"
        f"&client_id={SF_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope={SCOPES}"
        f"&code_challenge={challenge}"
        f"&code_challenge_method=S256"
        f"&state={state}"
    )

def handle_callback(code: str, state: str) -> dict:
    """
    Exchange the auth code from the OAuth callback for tokens.
    Saves tokens to auth_data.json. Call once after the browser redirect.
    """
    pkce = _load_pkce()
    if not pkce:
        raise SalesforceError("Session expired — please click Login again")
    if state != pkce.get("state"):
        raise SalesforceError("Invalid state — possible CSRF; try logging in again")

    payload = {
        "grant_type":    "authorization_code",
        "code":          code,
        "client_id":     pkce["client_id"],
        "redirect_uri":  REDIRECT_URI,
        "code_verifier": pkce["verifier"],
    }
    if pkce.get("client_secret"):
        payload["client_secret"] = pkce["client_secret"]

    resp = requests.post(
        f"{pkce['instance_url']}/services/oauth2/token",
        data=payload,
        timeout=30,
    )
    if resp.status_code != 200:
        raise SalesforceError(f"Token exchange failed: {resp.text}")

    tokens = resp.json()
    _save_token(
        refresh_token=tokens["refresh_token"],
        instance_url=tokens["instance_url"],
        access_token=tokens.get("access_token", ""),
        expires_in=tokens.get("expires_in", 3600),
    )
    _clear_pkce()
    return tokens


# ── Internal token getter (auto-refresh) ─────────────────────────────────────

def _get_token() -> tuple[str, str]:
    """Return (access_token, instance_url), refreshing automatically if needed."""
    stored = _load_token()
    if not stored:
        raise SalesforceError("NOT_AUTHENTICATED")

    instance_url = stored["instance_url"]

    # Return cached token if still valid
    if not _token_needs_refresh(stored) and stored.get("access_token"):
        return stored["access_token"], instance_url

    # Refresh
    data = {
        "grant_type":    "refresh_token",
        "client_id":     SF_CLIENT_ID,
        "refresh_token": stored["refresh_token"],
    }
    r = requests.post(f"{instance_url}/services/oauth2/token", data=data, timeout=30)
    if r.status_code != 200:
        raise SalesforceError(f"Token refresh failed ({r.status_code}): {r.text}")

    refreshed    = r.json()
    new_access   = refreshed["access_token"]
    new_instance = refreshed.get("instance_url", instance_url)
    new_refresh  = refreshed.get("refresh_token", stored["refresh_token"])
    _save_token(new_refresh, new_instance, new_access, refreshed.get("expires_in", 3600))
    return new_access, new_instance


# ── Public API (same signature as before) ────────────────────────────────────

def run_soql(date_from: str, time_from: str, date_to: str, time_to: str) -> list[dict]:
    """
    Fetch disconnected order items from Salesforce filtered by ActivatedDate range.
    date_from / date_to : YYYY-MM-DD
    time_from / time_to : HH:MM  (local — sent as-is; use UTC times if your SF org stores UTC)
    """
    access_token, instance_url = _get_token()

    # Build full ISO-8601 datetimes for the SOQL filter
    dt_from = f"{date_from}T{time_from}:00.000+0000"
    dt_to   = f"{date_to}T{time_to}:00.000+0000"

    soql = f"""
        SELECT id, order.id, PR_MSISDN__c, order.Account.Segment__c,
               vlocity_cmt__BillingAccountId__r.PR_Mobile_Billing_Number__c,
               product2.name, vlocity_cmt__Action__c,
               order.vlocity_cmt__Reason__c, CreatedDate, order.ActivatedDate,
               order.vlocity_cmt__OrderStatus__c, order.type, order.Account.name
        FROM orderitem
        WHERE PR_MSISDN__c != null
          AND product2.name != 'SIM Card'
          AND order.vlocity_cmt__Reason__c NOT IN ('Buyers Remorse', 'Inflight Cancel')
          AND order.type = 'Disconnect'
          AND vlocity_cmt__Action__c = 'Disconnect'
          AND order.ActivatedDate >= {dt_from}
          AND order.ActivatedDate <= {dt_to}
          AND vlocity_cmt__BillingAccountId__r.vlocity_cmt__AccountPaymentType__c != 'Prepaid'
        ORDER BY CreatedDate DESC
    """

    headers = {"Authorization": f"Bearer {access_token}"}
    url     = f"{instance_url}{QUERY_PATH}"
    params  = {"q": soql}
    records = []

    while url:
        resp   = requests.get(url, headers=headers, params=params, timeout=60)
        params = None  # only on first request

        if resp.status_code == 401:
            raise SalesforceError("NOT_AUTHENTICATED")
        if resp.status_code != 200:
            raise SalesforceError(f"SOQL failed ({resp.status_code}): {resp.text}")

        body = resp.json()
        records.extend(body.get("records", []))
        nxt = body.get("nextRecordsUrl")
        url = f"{instance_url}{nxt}" if nxt else None

    # Strip Salesforce metadata attribute blocks
    return [{k: v for k, v in r.items() if k != "attributes"} for r in records]


def test_connection() -> dict:
    """Quick connectivity check. Returns {"status": "ok", "instance_url": ...}."""
    token, instance_url = _get_token()
    resp = requests.get(
        f"{instance_url}/services/data/v59.0/",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if resp.status_code != 200:
        raise SalesforceError(f"Connection test failed: {resp.status_code}")
    return {"status": "ok", "instance_url": instance_url}
