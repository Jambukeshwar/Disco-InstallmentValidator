import requests
from aria_validator import ARIA_CLIENT_NO, ARIA_AUTH_KEY, ARIA_URL, HEADERS


def _sf_dt(date: str, time: str) -> str:
    """Format date+time as Salesforce datetime string: 2025-01-21T16:38:00.000+0000"""
    return f"{date}T{time}:00.000+0000"


def build_b2c_soql(date_from: str, date_to: str, time_from: str = "00:00", time_to: str = "23:59") -> str:
    dt_from = _sf_dt(date_from, time_from)
    dt_to   = _sf_dt(date_to,   time_to)
    return (
        "SELECT orderid, order.vlocity_cmt__OrderStatus__c, order.vlocity_cmt__Reason__c, "
        "vlocity_cmt__ServiceAccountId__c, order.type, PR_Original_OLI_ID__c, "
        "vlocity_cmt__ServiceAccountId__r.MSISDN__c, "
        "vlocity_cmt__BillingAccountId__r.PR_Mobile_Billing_Number__c, "
        "product2.name, Contract_Term__c, PRB2C_Installment_ID__c, vlocity_cmt__Action__c "
        "FROM orderitem "
        f"WHERE CreatedDate >= {dt_from} AND CreatedDate <= {dt_to} "
        "AND vlocity_cmt__Action__c = 'Add' "
        "AND Contract_Term__c != null "
        "AND Contract_Term__c != 'No Commit Retail Price' "
        "AND vlocity_cmt__BillingAccountId__r.Segment__c = 'B2C' "
        "AND order.vlocity_cmt__OrderStatus__c = 'Activated' "
        "AND Order.vlocity_cmt__Reason__c != 'Remove FMC Benefits' "
        "AND order.Type != 'Disconnect'"
    )


def build_b2b_soql(date_from: str, date_to: str, time_from: str = "00:00", time_to: str = "23:59") -> str:
    dt_from = _sf_dt(date_from, time_from)
    dt_to   = _sf_dt(date_to,   time_to)
    return (
        "SELECT orderid, order.vlocity_cmt__OrderStatus__c, order.vlocity_cmt__Reason__c, "
        "vlocity_cmt__ServiceAccountId__c, PR_Original_OLI_ID__c, "
        "vlocity_cmt__ServiceAccountId__r.MSISDN__c, "
        "vlocity_cmt__BillingAccountId__r.PR_Mobile_Billing_Number__c, "
        "product2.name, Contract_Term__c, PRB2B_InstallmentPlanID__c, vlocity_cmt__Action__c "
        "FROM orderitem "
        f"WHERE CreatedDate >= {dt_from} AND CreatedDate <= {dt_to} "
        "AND vlocity_cmt__Action__c = 'Add' "
        "AND PRB2B_InstallmentPlanID__c != null "
        "AND vlocity_cmt__BillingAccountId__r.Segment__c = 'B2B' "
        "AND order.vlocity_cmt__OrderStatus__c = 'Activated'"
    )


def _nested(record: dict, *keys):
    """Safely traverse nested SF response dicts."""
    val = record
    for k in keys:
        if not isinstance(val, dict):
            return None
        val = val.get(k)
    return val


def parse_sf_records(raw_records: list[dict], segment: str) -> list[dict]:
    """Convert raw SF API records into normalised dicts."""
    out = []
    for r in raw_records:
        inst_id_field = "PRB2C_Installment_ID__c" if segment == "B2C" else "PRB2B_InstallmentPlanID__c"
        out.append({
            "segment":           segment,
            "order_id":          r.get("OrderId") or r.get("Id"),
            "oli_id":            r.get("PR_Original_OLI_ID__c"),
            "sf_bancan":         _nested(r, "vlocity_cmt__BillingAccountId__r", "PR_Mobile_Billing_Number__c"),
            "msisdn":            _nested(r, "vlocity_cmt__ServiceAccountId__r", "MSISDN__c"),
            "product_name":      _nested(r, "Product2", "Name"),
            "order_reason":      _nested(r, "Order", "vlocity_cmt__Reason__c"),
            "contract_term":     r.get("Contract_Term__c"),
            "sf_installment_id": str(r.get(inst_id_field) or "").strip(),
        })
    return out


def check_aria(oli_id: str) -> dict:
    """Call Aria for one OLI ID and return parsed result."""
    payload = {
        "rest_call":       "get_plan_instance_information_m",
        "releaseVersion":  "52",
        "output_format":   "json",
        "client_no":       int(ARIA_CLIENT_NO),
        "auth_key":        ARIA_AUTH_KEY,
        "query_string":    f"client_plan_instance_id={oli_id}",
    }
    try:
        resp = requests.post(ARIA_URL, json=payload, headers=HEADERS, timeout=30)
        data = resp.json()

        if data.get("error_code") != 0 or not data.get("plan_instance_details"):
            return {
                "aria_found":          False,
                "aria_oli":            None,
                "aria_bancan":         None,
                "aria_installment_id": None,
                "aria_plan_name":      None,
                "error":               data.get("error_msg", "Not found"),
            }

        detail = data["plan_instance_details"][0]

        # Extract installment ID: product_field_value is pipe-separated, index 5
        aria_installment_id = None
        for pf in detail.get("product_fields", []):
            parts = (pf.get("product_field_value") or "").split("|")
            if len(parts) > 5 and parts[5].strip():
                aria_installment_id = parts[5].strip()
                break

        return {
            "aria_found":          True,
            "aria_oli":            detail.get("client_plan_instance_id"),
            "aria_bancan":         detail.get("client_acct_id"),
            "aria_installment_id": aria_installment_id,
            "aria_plan_name":      detail.get("plan_name"),
            "error":               None,
        }
    except Exception as exc:
        return {
            "aria_found":          False,
            "aria_oli":            None,
            "aria_bancan":         None,
            "aria_installment_id": None,
            "aria_plan_name":      None,
            "error":               str(exc),
        }


def process_single(record: dict) -> dict:
    """Validate one SF record against Aria."""
    oli_id           = (record.get("oli_id") or "").strip()
    sf_installment_id = record.get("sf_installment_id", "")

    if not oli_id:
        return {**record, "aria_oli": None, "aria_bancan": None,
                "aria_installment_id": None, "aria_plan_name": None,
                "validation": "❌", "match_status": "No Device Found in Aria", "error": "OLI ID missing in SF"}

    aria = check_aria(oli_id)

    if not aria["aria_found"]:
        validation   = "❌"
        match_status = "No Device Found in Aria"
    else:
        bancan_match = (aria["aria_bancan"] or "") == (record.get("sf_bancan") or "")
        inst_match   = (aria["aria_installment_id"] or "") == (sf_installment_id or "")
        if bancan_match and inst_match:
            validation   = "✅"
            match_status = "Clean"
        else:
            validation   = "⚠️"
            match_status = "Device Exist with wrong Inst ID/BANCAN"

    return {
        **record,
        "aria_oli":            aria["aria_oli"],
        "aria_bancan":         aria["aria_bancan"],
        "aria_installment_id": aria["aria_installment_id"],
        "aria_plan_name":      aria["aria_plan_name"],
        "validation":          validation,
        "match_status":        match_status,
        "error":               aria["error"],
    }
