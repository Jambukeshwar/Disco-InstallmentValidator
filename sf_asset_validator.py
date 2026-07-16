"""
Salesforce Asset Validator.
For each MSISDN, queries the SF Asset object to check provisioning status,
and cross-validates BAN-CAN and Disconnection Reason against the original order.

SOQL:
    SELECT id,
           vlocity_cmt__BillingAccountId__r.PR_Mobile_Billing_Number__c,
           PR_MSISDN__c,
           vlocity_cmt__ProvisioningStatus__c,
           Disconnection_Reason__c,
           vlocity_cmt__AssetReferenceId__c,
           vlocity_cmt__RootItemId__c,
           Product2.name
    FROM asset
    WHERE PR_MSISDN__c = '{msisdn}'
      AND product2.name != 'SIM Card'

Validation:
    ✅  Clean          — ProvisioningStatus in (Deleted, Disconnected)
                         AND BAN-CAN matches order
                         AND Disconnection_Reason matches order
    ⚠️  Action Needed  — Status not disconnected / BANCAN_Mismatch / Reason_Not_Matched
    ❌  Error          — MSISDN not found in SF assets / API error
"""

import requests
from salesforce import _get_token, SalesforceError

QUERY_PATH = "/services/data/v59.0/query"

CLEAN_STATUSES = {"deleted", "disconnected"}


def fetch_asset(msisdn: str) -> list[dict]:
    """Query SF asset records for a given MSISDN (excludes SIM Card)."""
    access_token, instance_url = _get_token()

    soql = f"""
        SELECT id,
               vlocity_cmt__BillingAccountId__r.PR_Mobile_Billing_Number__c,
               PR_MSISDN__c,
               vlocity_cmt__ProvisioningStatus__c,
               Disconnection_Reason__c,
               vlocity_cmt__AssetReferenceId__c,
               vlocity_cmt__RootItemId__c,
               Product2.name,
               LastModifiedDate
        FROM asset
        WHERE PR_MSISDN__c = '{msisdn}'
          AND product2.name != 'SIM Card'
        ORDER BY LastModifiedDate DESC
        LIMIT 1
    """

    headers = {"Authorization": f"Bearer {access_token}"}
    url     = f"{instance_url}{QUERY_PATH}"
    params  = {"q": soql}
    records = []

    while url:
        resp   = requests.get(url, headers=headers, params=params, timeout=60)
        params = None

        if resp.status_code == 401:
            raise SalesforceError("NOT_AUTHENTICATED")
        if resp.status_code != 200:
            raise SalesforceError(f"Asset SOQL failed ({resp.status_code}): {resp.text}")

        body = resp.json()
        records.extend(body.get("records", []))
        nxt = body.get("nextRecordsUrl")
        url = f"{instance_url}{nxt}" if nxt else None

    return [{k: v for k, v in r.items() if k != "attributes"} for r in records]


def _extract_ban(record: dict) -> str:
    """Safely extract BAN-CAN from nested SF response."""
    billing = record.get("vlocity_cmt__BillingAccountId__r") or {}
    if isinstance(billing, dict):
        return billing.get("PR_Mobile_Billing_Number__c") or "N/A"
    return "N/A"


def process_single(msisdn: str, order_ban: str = None, order_reason: str = None) -> dict:
    """
    Fetch SF asset for one MSISDN and validate.

    Args:
        msisdn       : The subscriber number to check
        order_ban    : BAN-CAN from the original SF order (for cross-check)
        order_reason : Disconnection reason from the original SF order (for cross-check)
    """
    base = {
        "msisdn":              msisdn,
        "sf_asset_id":         None,
        "sf_ban_can":          None,
        "sf_provisioning":     None,
        "sf_disc_reason":      None,
        "sf_product":          None,
        "sf_asset_ref_id":     None,
        "sf_root_item_id":     None,
        "sf_last_modified":    None,
        "sf_validation":       None,
        "sf_field_to_review":  None,
        "sf_error":            None,
    }

    try:
        records = fetch_asset(msisdn)

        if not records:
            return {
                **base,
                "sf_error":       "MSISDN not found in SF assets",
                "sf_validation":  "❌",
                "sf_field_to_review": "Asset_Not_Found",
            }

        # Take the first non-SIM asset record
        rec = records[0]

        sf_ban        = _extract_ban(rec)
        sf_status     = rec.get("vlocity_cmt__ProvisioningStatus__c") or "N/A"
        sf_reason     = rec.get("Disconnection_Reason__c") or "N/A"
        product_name  = (rec.get("Product2") or {}).get("name") or "N/A"

        base["sf_asset_id"]     = rec.get("Id")
        base["sf_ban_can"]      = sf_ban
        base["sf_provisioning"] = sf_status
        base["sf_disc_reason"]  = sf_reason
        base["sf_product"]      = product_name
        base["sf_asset_ref_id"] = rec.get("vlocity_cmt__AssetReferenceId__c")
        base["sf_root_item_id"] = rec.get("vlocity_cmt__RootItemId__c")
        base["sf_last_modified"] = rec.get("LastModifiedDate")

        review = []

        # BAN-CAN cross-check first — skip validation entirely if mismatched
        if order_ban and sf_ban != "N/A":
            if str(order_ban).strip() != str(sf_ban).strip():
                base["sf_field_to_review"] = "BANCAN_Mismatch"
                base["sf_validation"]      = "✅"
                return base

        # 1. Provisioning status check
        if sf_status.lower() not in CLEAN_STATUSES:
            review.append("Provisioning_Not_Disconnected")

        # 2. Disconnection reason cross-check
        if order_reason and sf_reason != "N/A":
            if str(order_reason).strip().lower() != str(sf_reason).strip().lower():
                review.append("Reason_Not_Matched")

        if not review:
            review.append("No_Action_Required")

        base["sf_field_to_review"] = "|".join(review)
        base["sf_validation"]      = "✅" if "No_Action_Required" in review else "⚠️"

        return base

    except SalesforceError as e:
        msg = str(e)
        return {
            **base,
            "sf_error":      msg,
            "sf_validation": "❌" if "NOT_AUTHENTICATED" not in msg else "auth_required",
            "sf_field_to_review": "SF_Error",
        }
    except Exception as e:
        return {
            **base,
            "sf_error":           str(e),
            "sf_validation":      "❌",
            "sf_field_to_review": "SF_Error",
        }
