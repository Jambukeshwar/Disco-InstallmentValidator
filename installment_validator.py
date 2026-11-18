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
        f"WHERE order.ActivatedDate >= {dt_from} AND order.ActivatedDate <= {dt_to} "
        "AND vlocity_cmt__Action__c = 'Add' "
        "AND Contract_Term__c != null "
        "AND Contract_Term__c != 'No Commit Retail Price' "
        "AND vlocity_cmt__BillingAccountId__r.Segment__c IN ('B2C','B2BSOHO') "
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
        f"WHERE order.ActivatedDate >= {dt_from} AND order.ActivatedDate <= {dt_to} "
        "AND vlocity_cmt__Action__c = 'Add' "
        "AND PRB2B_InstallmentPlanID__c != null "
        "AND vlocity_cmt__BillingAccountId__r.Segment__c = 'B2B' "
        "AND order.vlocity_cmt__OrderStatus__c = 'Activated' "
        "AND Order.vlocity_cmt__Reason__c NOT IN ('Remove FMC Benefits','Full Pay Off of Installment','Full Payoff of Installment - Next Invoice','Change Plan')"
    )


def _normalise_segment(segment) -> str:
    """B2BSoho is handled as B2C."""
    segment = (segment or "").strip()
    return "B2C" if segment.upper() == "B2BSOHO" else segment


def _sf_has_installment(rec: dict) -> bool:
    """B2B: PRB2B installment ID present. B2C (incl. B2BSoho): contract term set and not No Commit Retail Price."""
    contract = (rec.get("contract_term") or "").strip()
    inst_id  = (rec.get("sf_installment_id") or "").strip()
    if rec.get("segment") == "B2B":
        return bool(inst_id)
    return bool(contract) and contract != "No Commit Retail Price"


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
        f"WHERE order.ActivatedDate >= {dt_from} AND order.ActivatedDate <= {dt_to} "
        "AND Product2.vlocity_cmt__ObjectTypeName__c IN ('Mobile Phone','Tablet','Accessory','Smart Watch') "
        "AND order.vlocity_cmt__OrderStatus__c = 'Activated' "
        "AND Order.vlocity_cmt__Reason__c IN ('Change Plan') "
        "AND vlocity_cmt__BillingAccountId__r.vlocity_cmt__AccountPaymentType__c = 'Postpaid'"
    )


def build_order_soql(order_id: str) -> str:
    """Fetch installment-bearing order line items for a given SF Order ID."""
    return (
        "SELECT OrderId, order.Type, PR_Original_OLI_ID__c, "
        "vlocity_cmt__ServiceAccountId__r.MSISDN__c, "
        "vlocity_cmt__BillingAccountId__r.PR_Mobile_Billing_Number__c, "
        "Product2.Name, Contract_Term__c, "
        "PRB2C_Installment_ID__c, PRB2B_InstallmentPlanID__c, "
        "vlocity_cmt__Action__c, Order.vlocity_cmt__Reason__c, "
        "vlocity_cmt__BillingAccountId__r.Segment__c "
        "FROM OrderItem "
        "WHERE Product2.vlocity_cmt__ObjectTypeName__c "
        "IN ('Mobile Phone','Tablet','Accessory','Smart Watch') "
        f"AND OrderId = '{order_id}'"
    )


def parse_order_records(raw_records: list) -> tuple:
    """Parse SF records fetched by Order ID.

    Groups by OrderId. Orders with reason 'Change Plan' go to changeplan_pairs (a pair may
    have only one side, which process_changeplan_order flags). Other reasons validate the
    Add OLI alone.
    Returns (single_records, changeplan_pairs, skipped_records):
      - single_records   → validated with process_single
      - changeplan_pairs → validated with process_changeplan_order
      - skipped_records  → pre-built result dicts inserted directly (no Aria call needed)

    Add OLI installment check (non Change Plan):
      B2B → PRB2B_InstallmentPlanID__c present
      B2C → Contract_Term__c present and != 'No Commit Retail Price'
    If Add has no installment → stored as skipped with reason, not silently dropped.
    """
    from collections import defaultdict
    orders = defaultdict(list)
    for r in raw_records:
        order_id      = r.get("OrderId") or r.get("Id")
        segment       = _normalise_segment(_nested(r, "vlocity_cmt__BillingAccountId__r", "Segment__c")) or "B2C"
        inst_id_field = "PRB2C_Installment_ID__c" if segment == "B2C" else "PRB2B_InstallmentPlanID__c"
        orders[order_id].append({
            "segment":           segment,
            "order_id":          order_id,
            "oli_id":            r.get("PR_Original_OLI_ID__c"),
            "sf_bancan":         _nested(r, "vlocity_cmt__BillingAccountId__r", "PR_Mobile_Billing_Number__c"),
            "msisdn":            _nested(r, "vlocity_cmt__ServiceAccountId__r", "MSISDN__c"),
            "product_name":      _nested(r, "Product2", "Name"),
            "order_reason":      _nested(r, "Order", "vlocity_cmt__Reason__c"),
            "contract_term":     r.get("Contract_Term__c"),
            "sf_installment_id": str(r.get(inst_id_field) or "").strip(),
            "action":            r.get("vlocity_cmt__Action__c", ""),
        })

    single_records   = []
    changeplan_pairs = []
    skipped_records  = []

    def _skipped(rec, reason):
        return {
            "segment":           rec.get("segment"),
            "order_id":          rec.get("order_id"),
            "oli_id":            rec.get("oli_id"),
            "sf_bancan":         rec.get("sf_bancan"),
            "msisdn":            rec.get("msisdn"),
            "product_name":      rec.get("product_name"),
            "order_reason":      rec.get("order_reason"),
            "contract_term":     rec.get("contract_term"),
            "sf_installment_id": rec.get("sf_installment_id"),
            "aria_bancan":       None, "aria_oli": None,
            "aria_installment_id": None, "aria_plan_name": None,
            "device_amount":     None, "monthly_amount": None,
            "validation":        "✅",
            "match_status":      reason,
            "error":             None,
        }

    for recs in orders.values():
        disc = next((r for r in recs if r["action"] == "Disconnect"), None)
        add  = next((r for r in recs if r["action"] == "Add"), None)

        is_change_plan = any(r.get("order_reason") == "Change Plan" for r in recs)

        if is_change_plan:
            # Change Plan: every order is judged from Aria; lone Add / Disconnect lines are flagged
            if disc or add:
                changeplan_pairs.append({"disconnect": disc, "add": add})
        elif add:
            if _sf_has_installment(add):
                single_records.append(add)
            else:
                skipped_records.append(_skipped(add, "Skipped - No Installment on Add OLI"))
        # Disconnect-only rows of other reasons are skipped silently

    return single_records, changeplan_pairs, skipped_records


def parse_changeplan_records(raw_records: list) -> list:
    """Group SF change plan rows by OrderId into disconnect+add pairs (either side may be None).
    Segment is read from Segment__c on each row; installment ID picked accordingly."""
    from collections import defaultdict
    orders = defaultdict(list)
    for r in raw_records:
        order_id = r.get("OrderId") or r.get("Id")
        segment  = _normalise_segment(_nested(r, "vlocity_cmt__BillingAccountId__r", "Segment__c")) or "B2C"
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
        if disc or add:
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
        resp = requests.post(ARIA_URL, json=payload, headers=HEADERS, timeout=60)
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
        return {"aria_found": False, "aria_api_error": True, "aria_oli": None, "aria_bancan": None,
                "aria_installment_id": None, "aria_plan_name": None,
                "device_amount": None, "monthly_amount": None,
                "product_field_value": None, "error": str(exc)}


def _pos_equal(a: str, b: str) -> bool:
    a, b = (a or "").strip(), (b or "").strip()
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except (ValueError, TypeError):
        return False


def _aria_fields(aria: dict) -> dict:
    return {
        "aria_oli":            aria.get("aria_oli"),
        "aria_bancan":         aria.get("aria_bancan"),
        "aria_installment_id": aria.get("aria_installment_id"),
        "aria_plan_name":      aria.get("aria_plan_name"),
        "device_amount":       aria.get("device_amount"),
        "monthly_amount":      aria.get("monthly_amount"),
    }


def process_changeplan_order(pair: dict) -> dict:
    """Validate one Change Plan order from the Aria state of the old (Disconnect) and new (Add) device.

    SF Disconnect lines always carry No Commit Retail Price, so whether the old device had an
    installment comes from Aria only. Every pair produces a row; nothing is dropped silently.
    """
    disc_rec = pair.get("disconnect")
    add_rec  = pair.get("add")
    rec      = add_rec or disc_rec or {}

    base = {
        "segment":           rec.get("segment"),
        "order_id":          rec.get("order_id"),
        "oli_id":            (rec.get("oli_id") or "").strip() or None,
        "sf_bancan":         rec.get("sf_bancan") or "",
        "msisdn":            rec.get("msisdn"),
        "product_name":      rec.get("product_name"),
        "order_reason":      rec.get("order_reason") or "Change Plan",
        "contract_term":     rec.get("contract_term"),
        "sf_installment_id": rec.get("sf_installment_id"),
    }
    no_aria = {"aria_oli": None, "aria_bancan": None, "aria_installment_id": None,
               "aria_plan_name": None, "device_amount": None, "monthly_amount": None}

    def _row(validation, match_status, aria=None, error=None):
        return {**base, **(_aria_fields(aria) if aria else no_aria),
                "validation": validation, "match_status": match_status, "error": error}

    if disc_rec is None:
        return _row("⚠️", "Flag - SF Disconnect OLI not found")
    if add_rec is None:
        return _row("⚠️", "Flag - SF Add OLI not found")

    disc_oli  = (disc_rec.get("oli_id") or "").strip()
    add_oli   = (add_rec.get("oli_id") or "").strip()
    sf_bancan = add_rec.get("sf_bancan") or ""

    if not disc_oli:
        return _row("❌", "Disconnect OLI Missing in SF", error="No disconnect OLI ID")
    if not add_oli:
        return _row("❌", "Add OLI Missing in SF", error="No add OLI ID")

    # Old device: an installment is "running" only if found, balance >= 1 and status active / change plan / blank
    disc_aria = check_aria_full(disc_oli)
    if disc_aria.get("aria_api_error"):
        return _row("❌", "Aria API Error (Disconnect OLI)", error=disc_aria.get("error"))

    disc_parts  = []
    old_running = False
    if disc_aria["aria_found"]:
        disc_parts = (disc_aria.get("product_field_value") or "").split("|")
        try:
            balance = float(disc_parts[2].strip()) if len(disc_parts) > 2 and disc_parts[2].strip() else 0
        except (ValueError, TypeError):
            balance = 0
        status      = disc_parts[9].strip().lower() if len(disc_parts) > 9 else ""
        old_running = balance >= 1 and status in ("active", "change plan", "")

    # New device
    add_aria = check_aria_full(add_oli)
    if add_aria.get("aria_api_error"):
        return _row("❌", "Aria API Error (Add OLI)", error=add_aria.get("error"))

    if not add_aria["aria_found"]:
        if old_running:
            return _row("⚠️", "Flag - Installment not added during change plan")
        if _sf_has_installment(add_rec):
            return _row("⚠️", "Flag - Installment in SF but Add OLI not found in Aria")
        return _row("✅", "Clean - No installment before and after change plan")

    if (add_aria.get("aria_bancan") or "") != sf_bancan:
        return _row("⚠️", "Flag - BAN-CAN mismatch", add_aria)

    if not old_running:
        return _row("⚠️", "Flag - Installment newly added during change plan", add_aria)

    add_parts = (add_aria.get("product_field_value") or "").split("|")
    string_match = all(
        _pos_equal(
            disc_parts[i] if i < len(disc_parts) else "",
            add_parts[i]  if i < len(add_parts)  else ""
        )
        for i in range(6)
    )
    if string_match:
        return _row("✅", "Clean - Installment added without issue", add_aria)
    return _row("⚠️", "Flag - String mismatched after change plan", add_aria)


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
        resp = requests.post(ARIA_URL, json=payload, headers=HEADERS, timeout=60)
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
