import os
import io
import base64
import requests
from dotenv import load_dotenv

load_dotenv()

JIRA_BASE_URL  = os.getenv("JIRA_BASE_URL",  "https://lla.atlassian.net")
JIRA_EMAIL     = os.getenv("JIRA_EMAIL",     "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")

_ISSUE_URL      = f"{JIRA_BASE_URL}/rest/api/3/issue"
_ATTACH_URL_TPL = f"{JIRA_BASE_URL}/rest/api/3/issue/{{key}}/attachments"

# All IDs resolved from the SAC project's Incident SAC issue type (id 11557)
_FIXED_FIELDS = {
    "project":           {"id": "10145"},   # SAC
    "issuetype":         {"id": "11557"},   # Incident SAC
    "customfield_10051": {"id": "12940"},   # Severity: Sev-4
    "customfield_10897": {"id": "12606"},   # Type of Payment: Postpaid
    "customfield_10880": {"id": "12576"},   # Account type: B2C
    "customfield_10873": {"id": "12544"},   # Customer has Voice Service: Yes
    "customfield_10874": {"id": "12560"},   # Customer has Data Service: Yes
    "customfield_10861": {"id": "37019"},   # Reporting Area: Monitoring team
    "customfield_10867": {"id": "12378"},   # Incident Category: Salesforce
    "customfield_10868": {"id": "29141"},   # Incident Sub Category: Installment related error
    "customfield_10872": {"id": "12572"},   # Migrated Customer: No
    "customfield_10863": "N/A",             # Device Model (text)
    "customfield_10860": 0,                 # Customer MSISDN (number placeholder)
    "priority":          {"id": "11027"},   # Priority: 4- Regular
}


def _auth_header() -> str:
    return "Basic " + base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()


def _json_headers() -> dict:
    return {
        "Authorization": _auth_header(),
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }


def _build_adf(text: str) -> dict:
    """Convert plain text (newline-separated) into Atlassian Document Format."""
    paragraphs = []
    for line in text.split("\n"):
        paragraphs.append({
            "type": "paragraph",
            "content": [{"type": "text", "text": line if line else " "}],
        })
    return {"type": "doc", "version": 1, "content": paragraphs}


def create_sac_ticket(summary: str, description: str) -> str:
    """Create a SAC Incident SAC ticket. Returns the issue key e.g. SAC-1234."""
    if not JIRA_EMAIL or not JIRA_API_TOKEN:
        raise RuntimeError("JIRA_EMAIL or JIRA_API_TOKEN not configured in .env")

    payload = {
        "fields": {
            **_FIXED_FIELDS,
            "summary":     summary,
            "description": _build_adf(description),
        }
    }
    resp = requests.post(_ISSUE_URL, json=payload, headers=_json_headers(), timeout=30)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Jira create failed ({resp.status_code}): {resp.text}")
    key = resp.json()["key"]
    print(f"[jira] Ticket created: {key}")
    return key


def attach_file(issue_key: str, filename: str, file_bytes: bytes,
                content_type: str = "application/octet-stream"):
    """Attach a file to an existing Jira issue."""
    headers = {
        "Authorization":       _auth_header(),
        "X-Atlassian-Token":   "no-check",
    }
    files = {"file": (filename, io.BytesIO(file_bytes), content_type)}
    url   = _ATTACH_URL_TPL.format(key=issue_key)
    resp  = requests.post(url, headers=headers, files=files, timeout=60)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Jira attach failed ({resp.status_code}): {resp.text}")
    print(f"[jira] Attached {filename} to {issue_key}")


def create_ticket_with_attachment(summary: str, description: str,
                                   filename: str, file_bytes: bytes) -> str:
    """Create a SAC ticket and attach a file. Returns the issue key."""
    key = create_sac_ticket(summary, description)
    attach_file(key, filename, file_bytes)
    return key
