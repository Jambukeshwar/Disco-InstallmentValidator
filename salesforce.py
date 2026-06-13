"""
Salesforce REST API client — SOAP username/password login.
Credentials are read from .env (SF_USERNAME, SF_PASSWORD, SF_SECURITY_TOKEN).
Token is cached in memory and refreshed automatically every 110 minutes.
"""

import os
import re
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

SF_USERNAME        = os.getenv("SF_USERNAME", "")
SF_PASSWORD        = os.getenv("SF_PASSWORD", "")
SF_SECURITY_TOKEN  = os.getenv("SF_SECURITY_TOKEN", "")
SF_INSTANCE_URL    = os.getenv("SF_INSTANCE_URL", "https://cwc.my.salesforce.com")

SOAP_LOGIN_URL = "https://login.salesforce.com/services/Soap/u/59.0"
QUERY_PATH     = "/services/data/v59.0/query"

# In-memory token cache
_cache: dict = {"access_token": None, "instance_url": None, "expires_at": None}


class SalesforceError(Exception):
    pass


# ── SOAP login ────────────────────────────────────────────────────────────────

def _soap_login() -> tuple[str, str]:
    """Login via SOAP partner API and return (session_id, instance_url)."""
    combined_password = SF_PASSWORD + SF_SECURITY_TOKEN
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
        ' xmlns:urn="urn:partner.soap.sforce.com">'
        "<soapenv:Body><urn:login>"
        f"<urn:username>{SF_USERNAME}</urn:username>"
        f"<urn:password>{combined_password}</urn:password>"
        "</urn:login></soapenv:Body></soapenv:Envelope>"
    )
    headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": "login"}
    resp = requests.post(SOAP_LOGIN_URL, data=body.encode("utf-8"), headers=headers, timeout=30)

    text = resp.text
    if resp.status_code != 200:
        fault = re.search(r"<faultstring>(.*?)</faultstring>", text)
        msg = fault.group(1) if fault else text
        raise SalesforceError(f"SOAP login failed: {msg}")

    session_match = re.search(r"<sessionId>(.*?)</sessionId>", text)
    if not session_match:
        fault = re.search(r"<faultstring>(.*?)</faultstring>", text)
        msg = fault.group(1) if fault else "No sessionId in response"
        raise SalesforceError(f"SOAP login fault: {msg}")

    session_id = session_match.group(1)

    server_match = re.search(r"<serverUrl>(.*?)</serverUrl>", text)
    if server_match:
        m = re.match(r"(https://[^/]+)", server_match.group(1))
        instance_url = m.group(1) if m else SF_INSTANCE_URL
    else:
        instance_url = SF_INSTANCE_URL

    return session_id, instance_url


# ── Token getter (cached, auto-refresh) ───────────────────────────────────────

def _get_token() -> tuple[str, str]:
    """Return (access_token, instance_url), re-logging in via SOAP when expired."""
    global _cache
    now = datetime.now(timezone.utc)

    if (_cache["access_token"] and _cache["expires_at"] and now < _cache["expires_at"]):
        return _cache["access_token"], _cache["instance_url"]

    session_id, instance_url = _soap_login()
    _cache = {
        "access_token": session_id,
        "instance_url": instance_url,
        "expires_at":   now + timedelta(minutes=110),
    }
    return session_id, instance_url


def is_authenticated() -> bool:
    """Return True if credentials are configured and a login succeeds."""
    if not SF_USERNAME or not SF_PASSWORD:
        return False
    try:
        _get_token()
        return True
    except Exception:
        return False


def clear_token():
    """Invalidate the cached token (next call will re-login)."""
    global _cache
    _cache = {"access_token": None, "instance_url": None, "expires_at": None}


# ── Stubs kept for API compatibility with main.py ─────────────────────────────

def build_login_url() -> str:
    return ""


def handle_callback(code: str, state: str) -> None:
    pass


def _save_token(*args, **kwargs) -> None:
    pass


# ── SOQL query ────────────────────────────────────────────────────────────────

def run_soql(date_from: str, time_from: str, date_to: str, time_to: str) -> list[dict]:
    """
    Fetch disconnected order items from Salesforce filtered by ActivatedDate range.
    date_from / date_to : YYYY-MM-DD
    time_from / time_to : HH:MM
    """
    access_token, instance_url = _get_token()

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
        resp   = requests.get(url, headers=headers, params=params, timeout=180)
        params = None

        if resp.status_code == 401:
            # Session expired — clear cache and retry once
            clear_token()
            access_token, instance_url = _get_token()
            headers = {"Authorization": f"Bearer {access_token}"}
            resp = requests.get(url, headers=headers, timeout=180)

        if resp.status_code != 200:
            raise SalesforceError(f"SOQL failed ({resp.status_code}): {resp.text}")

        body = resp.json()
        records.extend(body.get("records", []))
        nxt = body.get("nextRecordsUrl")
        url = f"{instance_url}{nxt}" if nxt else None

    return [{k: v for k, v in r.items() if k != "attributes"} for r in records]


def run_soql_query(soql: str) -> list[dict]:
    """Execute any SOQL query and return all records (handles pagination)."""
    access_token, instance_url = _get_token()
    headers = {"Authorization": f"Bearer {access_token}"}
    url     = f"{instance_url}{QUERY_PATH}"
    params  = {"q": soql}
    records = []

    while url:
        resp   = requests.get(url, headers=headers, params=params, timeout=180)
        params = None

        if resp.status_code == 401:
            clear_token()
            access_token, instance_url = _get_token()
            headers = {"Authorization": f"Bearer {access_token}"}
            resp = requests.get(url, headers=headers, timeout=180)

        if resp.status_code != 200:
            raise SalesforceError(f"SOQL failed ({resp.status_code}): {resp.text}")

        body = resp.json()
        records.extend(body.get("records", []))
        nxt = body.get("nextRecordsUrl")
        url = f"{instance_url}{nxt}" if nxt else None

    return [{k: v for k, v in r.items() if k != "attributes"} for r in records]


def test_connection() -> dict:
    """Quick connectivity check."""
    token, instance_url = _get_token()
    resp = requests.get(
        f"{instance_url}/services/data/v59.0/",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if resp.status_code != 200:
        raise SalesforceError(f"Connection test failed: {resp.status_code}")
    return {"status": "ok", "instance_url": instance_url}
