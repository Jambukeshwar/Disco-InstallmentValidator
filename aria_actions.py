"""
Aria action executor for Disconnect Validation Suite.
Provides three corrective actions callable from the drill-down modal:
  1. cancel_plan        — update_acct_plan_multi_m  (cancel main plan instance)
  2. cancel_installment — update_acct_plan_multi_m  (cancel child/device plan)
  3. create_nso_order   — create_order_m            (raise NSO balance order)
"""

import json
import os
import requests
from dotenv import load_dotenv

load_dotenv()

ARIA_CLIENT_NO = int(os.getenv("ARIA_CLIENT_NO", "0"))
ARIA_AUTH_KEY  = os.getenv("ARIA_AUTH_KEY", "")
ARIA_URL_V1    = os.getenv("ARIA_URL_V1", "https://secure.ariasystems.net/v1/core")

HEADERS = {"Content-Type": "application/json"}
TIMEOUT = 30


def _post(payload: dict) -> dict:
    resp = requests.post(ARIA_URL_V1, headers=HEADERS, data=json.dumps(payload), timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# ── 1. Cancel main plan ───────────────────────────────────────────────────────

def cancel_plan(ban_can: str, plan_instance_id: str, caller_name: str) -> dict:
    """
    Cancel a main plan instance via update_acct_plan_multi_m.
    plan_status_cd = -2 (cancelled), plan_directive = 3, invoicing_option = 4.
    """
    payload = {
        "rest_call":            "update_acct_plan_multi_m",
        "output_format":        "json",
        "client_no":            ARIA_CLIENT_NO,
        "auth_key":             ARIA_AUTH_KEY,
        "client_acct_id":       ban_can,
        "assignment_directive": "3",
        "invoicing_option":     "4",
        "alt_caller_id":        caller_name,
        "plan_updates": [
            {
                "plan_directive":                  3,
                "existing_client_plan_instance_id": plan_instance_id,
                "plan_status_cd":                  -2,
                "proration_invoice_timing":         1,
                "invoice_unbilled_usage":           False,
            }
        ],
    }
    result = _post(payload)
    _assert_ok(result, "cancel_plan")
    return result


# ── 2. Cancel installment / device plan ──────────────────────────────────────

def cancel_installment(
    ban_can: str,
    child_plan_instance_id: str,
    order_reason: str,
    caller_name: str,
) -> dict:
    """
    Cancel a child (device/installment) plan via update_acct_plan_multi_m.
    plan_directive = 4, plan_status_cd = 1, invoicing_option = 4.
    """
    payload = {
        "rest_call":                        "update_acct_plan_multi_m",
        "output_format":                    "json",
        "client_no":                        ARIA_CLIENT_NO,
        "auth_key":                         ARIA_AUTH_KEY,
        "client_acct_id":                   ban_can,
        "plan_directive":                   4,
        "assignment_directive":             3,
        "invoicing_option":                 4,
        "existing_client_plan_instance_id": child_plan_instance_id,
        "plan_status_cd":                   1,
        "comments":                         order_reason,
        "alt_caller_id":                    caller_name,
    }
    result = _post(payload)
    _assert_ok(result, "cancel_installment")
    return result


# ── 3. Create NSO order ───────────────────────────────────────────────────────

def create_nso_order(
    ban_can: str,
    child_plan_instance_id: str,
    sku: str,
    amount: float,
    msisdn: str,
    installment_id: str,
) -> dict:
    """
    Raise an NSO installment balance order via create_order_m.
    bill_immediately = 0 (do not bill right away).
    """
    line_comment = (
        f"Remaining Installment Balance|{msisdn}| Installment ID: {installment_id}"
    )
    payload = {
        "rest_call":               "create_order_m",
        "output_format":           "json",
        "client_no":               ARIA_CLIENT_NO,
        "auth_key":                ARIA_AUTH_KEY,
        "client_acct_id":          ban_can,
        "client_plan_instance_id": child_plan_instance_id,
        "order_line_items": [
            {
                "client_sku":    sku,
                "units":         1,
                "amount":        amount,
                "line_comments": line_comment,
            }
        ],
        "bill_immediately": 0,
    }
    result = _post(payload)
    _assert_ok(result, "create_nso_order")
    return result


# ── 4. Activate plan ─────────────────────────────────────────────────────────

def activate_plan(ban_can: str, plan_instance_id: str, caller_name: str) -> dict:
    """
    Activate a plan instance via update_acct_plan_multi_m.
    plan_status_cd = 1 (active), plan_directive = 3, invoicing_option = 4.
    """
    payload = {
        "rest_call":            "update_acct_plan_multi_m",
        "output_format":        "json",
        "client_no":            ARIA_CLIENT_NO,
        "auth_key":             ARIA_AUTH_KEY,
        "client_acct_id":       ban_can,
        "assignment_directive": "3",
        "invoicing_option":     "4",
        "alt_caller_id":        caller_name,
        "plan_updates": [
            {
                "plan_directive":                  3,
                "existing_client_plan_instance_id": plan_instance_id,
                "plan_status_cd":                  1,
                "proration_invoice_timing":         1,
                "invoice_unbilled_usage":           False,
            }
        ],
    }
    result = _post(payload)
    _assert_ok(result, "activate_plan")
    return result


# ── Helper ────────────────────────────────────────────────────────────────────

def _assert_ok(result: dict, action: str):
    """Raise if Aria returned a non-OK error_msg."""
    err = result.get("error_msg", "")
    if err and err.upper() != "OK":
        raise ValueError(f"Aria {action} failed: {err}")