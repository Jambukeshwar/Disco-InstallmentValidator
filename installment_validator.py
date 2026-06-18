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
        "AND Order.vlocity_cmt__Reason__c NOT IN ('Remove FMC Benefits','Full Pay Off of Installment','Full Payoff of Installment - Next Invoice','Change Plan') "
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
        "AND order.vlocity_cmt__OrderStatus__c = 'Activated' "
        "AND Order.vlocity_cmt__Reason__c NOT IN ('Remove FMC Benefits','Full Pay Off of Installment','Full Payoff of Installment - Next Invoice','Change Plan')"
    )


def _nested(record: dict, *keys):
    """Safely traverse nested SF response dicts."""
    val = record
    for k in keys:
        if not isinstance(val, dict):
            return None
        val = val.get(k)
    return val


def build_changeplan_soql(date_from: str, date_to: str, time_from: str = "00:00", time_to: str = "23:59") -> str:
    dt_from = _sf_dt(date_from, time_from)
    dt_to   = _sf_dt(date_to,   time_to)
    return (
        "SELECT orderid, order.vlocity_cmt__OrderStatus__c, order.vlocity_cmt__Reason__c, "
        "vlocity_cmt__ServiceAccountId__c, order.type, PR_Original_OLI_ID__c, "
        "vlocity_cmt__ServiceAccountId__r.MSISDN__c, "
        "vlocity_cmt__BillingAccountId__r.PR_Mobile_Billing_Number__c, "
        "product2.name, Contract_Term__c, PRB2C_Installment_ID__c, PRB2B_InstallmentPlanID__c, "
        "vlocity_cmt__Action__c, vlocity_cmt__BillingAccountId__r.Segment__c "
        "FROM orderitem "
        f"WHERE CreatedDate >= {dt_from} AND CreatedDate <= {dt_to} "
        "AND Product2.vlocity_cmt__ObjectTypeName__c IN ('Mobile Phone','Tablet','Accessory','Smart Watch') "
        "AND order.vlocity_cmt__OrderStatus__c = 'Activated' "
        "AND Order.vlocity_cmt__Reason__c IN ('Change Plan')"
    )


def parse_changeplan_records(raw_records: list) -> list:
    """Group SF change plan rows by OrderId into disconnect+add pairs.
    Segment is read from Segment__c on each row; installment ID picked accordingly."""
    from collections import defaultdict
    orders = defaultdict(list)
    for r in raw_records:
        order_id = r.get("OrderId") or r.get("Id")
        segment  = _nested(r, "vlocity_cmt__BillingAccountId__r", "Segment__c") or ""
        inst_field = "PRB2C_Installment_ID__c" if segment == "B2C" else "PRB2B_InstallmentPlanID__c"
        orders[order_id].append({
            "order_id":          order_id,
            "action":            r.get("vlocity_cmt__Action__c", ""),
            "oli_id":            r.get("PR_Original_OLI_ID__c"),
            "sf_bancan":         _nested(r, "vlocity_cmt__BillingAccountId__r", "PR_Mobile_Billing_Number__c"),
            "msisdn":            _nested(r, "vlocity_cmt__ServiceAccountId__r", "MSISDN__c"),
            "product_name":      _nested(r, "Product2", "Name"),
            "contract_term":     r.get("Contract_Term__c"),
            "sf_installment_id": str(r.get(inst_field) or "").strip(),
            "segment":           segment,
        })
    pairs = []
    for recs in orders.values():
        disc = next((r for r in recs if r["action"] == "Disconnect"), None)
        add  = next((r for r in recs if r["action"] == "Add"), None)
        if disc and add:
            pairs.append({"disconnect": disc, "add": add})
    return pairs


def check_aria_full(oli_id: str) -> dict:
    """Call Aria for one OLI — returns same keys as check_aria() plus product_field_value string."""
    payload = {
        "rest_call":      "get_plan_instance_information_m",
        "releaseVersion": "52",
        "output_format":  "json",
        "client_no":      int(ARIA_CLIENT_NO),
        "auth_key":       ARIA_AUTH_KEY,
        "query_string":   f"client_plan_instance_id={oli_id}",
    }
    try:
        resp = requests.post(ARIA_URL, json=payload, headers=HEADERS, timeout=30)
        data = resp.json()
        error_code = data.get("error_code")
        if error_code != 0:
            return {"aria_found": False, "aria_api_error": True, "aria_oli": None, "aria_bancan": None,
                    "aria_installment_id": None, "aria_plan_name": None,
                    "device_amount": None, "monthly_amount": None,
                    "product_field_value": None, "error": f"Aria error {error_code}: {data.get('error_msg', '')}"}
        if not data.get("plan_instance_details"):
            return {"aria_found": False, "aria_api_error": False, "aria_oli": None, "aria_bancan": None,
                    "aria_installment_id": None, "aria_plan_name": None,
                    "device_amount": None, "monthly_amount": None,
                    "product_field_value": None, "error": "Not found"}
        detail = data["plan_instance_details"][0]
        product_field_value = None
        aria_installment_id = None
        device_amount       = None
        monthly_amount      = None
        for pf in detail.get("product_fields", []):
            pfv = (pf.get("product_field_value") or "").strip()
            if pfv:
                product_field_value = pfv
                parts = pfv.split("|")
                if len(parts) > 5:
                    device_amount       = parts[0].strip() or None
                    monthly_amount      = parts[4].strip() or None
                    aria_installment_id = parts[5].strip() or None
                break
        return {"aria_found": True,
                "aria_oli":            detail.get("client_plan_instance_id"),
                "aria_bancan":         detail.get("client_acct_id"),
                "aria_installment_id": aria_installment_id,
                "aria_plan_name":      detail.get("plan_name"),
                "device_amount":       device_amount,
                "monthly_amount":      monthly_amount,
                "product_field_value": product_field_value,
                "error":               None}
    except Exception as exc:
        return {"aria_found": False, "aria_oli": None, "aria_bancan": None,
                "aria_installment_id": None, "aria_plan_name": None,
                "device_amount": None, "monthly_amount": None,
                "product_field_value": None, "error": str(exc)}


def process_changeplan_order(pair: dict):
    """Validate one change plan order pair. Returns None if old device had no installment.
    Output format matches process_single — Add OLI data only, disconnect used internally."""
    disc_rec  = pair["disconnect"]
    add_rec   = pair["add"]
    disc_oli  = (disc_rec.get("oli_id") or "").strip()
    add_oli   = (add_rec.get("oli_id") or "").strip()
    sf_bancan = add_rec.get("sf_bancan") or ""

    # Base record using Add OLI as the canonical row (same shape as process_single output)
    base = {
        "segment":           add_rec.get("segment") or disc_rec.get("segment"),
        "order_id":          add_rec.get("order_id"),
        "oli_id":            add_oli or None,
        "sf_bancan":         sf_bancan,
        "msisdn":            add_rec.get("msisdn"),
        "product_name":      add_rec.get("product_name"),
        "order_reason":      "Change Plan",
        "contract_term":     add_rec.get("contract_term"),
        "sf_installment_id": add_rec.get("sf_installment_id"),
    }

    no_aria = {"aria_oli": None, "aria_bancan": None, "aria_installment_id": None,
               "aria_plan_name": None, "device_amount": None, "monthly_amount": None}

    if not disc_oli:
        return {**base, **no_aria,
                "validation": "❌", "match_status": "Disconnect OLI Missing in SF",
                "error": "No disconnect OLI ID"}

    disc_aria = check_aria_full(disc_oli)
    if not disc_aria["aria_found"]:
        if disc_aria.get("aria_api_error"):
            return {**base, **no_aria, "validation": "❌",
                    "match_status": "Aria API Error (Disconnect OLI)", "error": disc_aria.get("error")}
        return None  # Genuinely not found — old device had no installment

    disc_parts = (disc_aria.get("product_field_value") or "").split("|")

    # If 3rd position (index 2) is empty or < 1, installment is completed — skip
    try:
        disc_pos2 = float(disc_parts[2].strip()) if len(disc_parts) > 2 and disc_parts[2].strip() else 0
    except (ValueError, TypeError):
        disc_pos2 = 0
    if disc_pos2 < 1:
        return None

    # Skip if plan status is not active or change plan (e.g. cancelled, terminated)
    disc_pos9 = disc_parts[9].strip().lower() if len(disc_parts) > 9 else ""
    if disc_pos9 not in ("active", "change plan"):
        return None

    if not add_oli:
        return {**base, **no_aria,
                "validation": "❌", "match_status": "Add OLI Missing in SF",
                "error": "No add OLI ID"}

    add_aria = check_aria_full(add_oli)
    if not add_aria["aria_found"]:
        return {**base, **no_aria,
                "validation": "❌", "match_status": "New Installment Not Found in Aria",
                "error": add_aria.get("error")}

    add_parts    = (add_aria.get("product_field_value") or "").split("|")
    bancan_match = (add_aria.get("aria_bancan") or "") == sf_bancan

    def _pos_equal(a, b):
        a, b = a.strip(), b.strip()
        if a == b:
            return True
        try:
            return float(a) == float(b)
        except (ValueError, TypeError):
            return False

    string_match = all(
        _pos_equal(
            disc_parts[i] if i < len(disc_parts) else "",
            add_parts[i]  if i < len(add_parts)  else ""
        )
        for i in range(6)
    )

    if bancan_match and string_match:
        validation, match_status = "✅", "Clean"
    elif not bancan_match and not string_match:
        validation, match_status = "⚠️", "BAN-CAN & Installment Data Mismatch"
    elif not bancan_match:
        validation, match_status = "⚠️", "BAN-CAN Mismatch"
    else:
        validation, match_status = "⚠️", "Installment Data Mismatch"

    return {
        **base,
        "aria_oli":            add_aria.get("aria_oli"),
        "aria_bancan":         add_aria.get("aria_bancan"),
        "aria_installment_id": add_aria.get("aria_installment_id"),
        "aria_plan_name":      add_aria.get("aria_plan_name"),
        "device_amount":       add_aria.get("device_amount"),
        "monthly_amount":      add_aria.get("monthly_amount"),
        "validation":          validation,
        "match_status":        match_status,
        "error":               None,
    }


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
                "device_amount":       None,
                "monthly_amount":      None,
                "error":               data.get("error_msg", "Not found"),
            }

        detail = data["plan_instance_details"][0]

        # product_field_value is pipe-separated:
        # [0]=device_amount  [4]=monthly_amount  [5]=installment_id
        aria_installment_id = None
        device_amount       = None
        monthly_amount      = None
        for pf in detail.get("product_fields", []):
            parts = (pf.get("product_field_value") or "").split("|")
            if len(parts) > 5:
                device_amount       = parts[0].strip() or None
                monthly_amount      = parts[4].strip() or None
                aria_installment_id = parts[5].strip() or None
                break

        return {
            "aria_found":          True,
            "aria_oli":            detail.get("client_plan_instance_id"),
            "aria_bancan":         detail.get("client_acct_id"),
            "aria_installment_id": aria_installment_id,
            "aria_plan_name":      detail.get("plan_name"),
            "device_amount":       device_amount,
            "monthly_amount":      monthly_amount,
            "error":               None,
        }
    except Exception as exc:
        return {
            "aria_found":          False,
            "aria_oli":            None,
            "aria_bancan":         None,
            "aria_installment_id": None,
            "aria_plan_name":      None,
            "device_amount":       None,
            "monthly_amount":      None,
            "error":               str(exc),
        }


def process_single(record: dict) -> dict:
    """Validate one SF record against Aria."""
    oli_id           = (record.get("oli_id") or "").strip()
    sf_installment_id = record.get("sf_installment_id", "")

    if not oli_id:
        return {**record, "aria_oli": None, "aria_bancan": None,
                "aria_installment_id": None, "aria_plan_name": None,
                "device_amount": None, "monthly_amount": None,
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
        "device_amount":       aria.get("device_amount"),
        "monthly_amount":      aria.get("monthly_amount"),
        "validation":          validation,
        "match_status":        match_status,
        "error":               aria["error"],
    }
