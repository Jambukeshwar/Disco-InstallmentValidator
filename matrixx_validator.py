"""
Matrixx REST API validator.
Checks subscription, plan and device status per MSISDN for disconnect reconciliation.

Validation logic:
  ✅  Clean         — subscription not active AND no active plan AND device not found/not active
  ⚠️  Action Needed — subscription still active OR plan still active OR device still active
  ❌  Error          — API returned non-OK or request failed
"""

import os
import requests
import threading
from dotenv import load_dotenv

load_dotenv()

MATRIXX_BASE_URL = os.getenv("MATRIXX_BASE_URL", "http://localhost:8080/rsgateway/data/json")

PLAN_OFFER_TYPES = {"BasePlan", "TabletPlan", "DataPlan", "DataVoicePlan", "WearablePlan"}

TIMEOUT = 30


def fetch_subscription(msisdn: str) -> dict:
    url = f"{MATRIXX_BASE_URL}/subscription/AccessNumber+{msisdn}"
    resp = requests.get(url, timeout=TIMEOUT)
    return resp.json()


def fetch_device(msisdn: str) -> dict:
    url = f"{MATRIXX_BASE_URL}/device/AccessNumber+{msisdn}"
    resp = requests.get(url, timeout=TIMEOUT)
    return resp.json()


def process_single(msisdn: str, order_ban: str = None, activated_date: str = None) -> dict:
    base = {
        "msisdn":                    msisdn,
        "mtx_order_ban":             order_ban,
        "mtx_ban_can":               None,
        "mtx_segment":               None,
        "mtx_payment_pref":          None,
        "mtx_usage_guide":           None,
        "mtx_subscription":          None,
        "mtx_subscription_id":       None,
        "mtx_subscription_status":   None,
        "mtx_plan_name":             None,
        "mtx_plan_code":             None,
        "mtx_plan_status":           None,
        "mtx_plan_charge":           None,
        "mtx_resource_id":           None,
        "mtx_plan_activation_time":  None,
        "mtx_device_status":         None,
        "mtx_validation":            None,
        "mtx_field_to_review":       None,
        "mtx_error":                 None,
    }

    try:
        # Fetch subscription and device in parallel
        sub_result    = [None]
        device_result = [None]
        sub_err       = [None]

        def get_subscription():
            try:
                sub_result[0] = fetch_subscription(msisdn)
            except Exception as e:
                sub_err[0] = str(e)

        def get_device():
            try:
                device_result[0] = fetch_device(msisdn)
            except Exception:
                device_result[0] = {}

        t1 = threading.Thread(target=get_subscription)
        t2 = threading.Thread(target=get_device)
        t1.start(); t2.start()
        t1.join();  t2.join()

        if sub_err[0]:
            return {**base, "mtx_error": f"Request failed: {sub_err[0]}", "mtx_validation": "❌"}

        result        = sub_result[0]
        device_result = device_result[0] or {}

        # Subscription check
        if result.get("_resultText") != "OK":
            result_text = result.get("_resultText", "") or result.get("ResultText", "")
            if "not found" in result_text.lower():
                base["mtx_subscription_status"] = "Not Found"
            else:
                return {**base, "mtx_error": result_text, "mtx_validation": "❌"}
        else:
            attr = result.get("Attr", {})
            base["mtx_ban_can"]             = attr.get("LLACustomerAccountID")
            base["mtx_segment"]             = attr.get("LLACustomerSegment")
            base["mtx_payment_pref"]        = attr.get("LLAPaymentPreference", "N/A")
            base["mtx_usage_guide"]         = attr.get("LLAUsageGuideID", "N/A")
            base["mtx_subscription"]        = result.get("Name")
            base["mtx_subscription_id"]     = result.get("ExternalId")
            base["mtx_subscription_status"] = result.get("StatusDescription")

            offers = result.get("PurchasedOfferArray", [])
            plan_offer = None
            for offer in offers:
                offer_attr = offer.get("Attr", {})
                if "CommercialPlanName" in offer_attr and offer_attr.get("OfferType") in PLAN_OFFER_TYPES:
                    if plan_offer is None:
                        plan_offer = offer  # first match as fallback
                    if (offer.get("OfferStatusDescription") or "").lower() == "active":
                        plan_offer = offer  # prefer active plan
                        break

            if plan_offer:
                offer_attr = plan_offer.get("Attr", {})
                base["mtx_plan_name"]           = offer_attr.get("CommercialPlanName")
                base["mtx_plan_code"]           = offer_attr.get("CommercialPlanCode")
                base["mtx_plan_status"]         = plan_offer.get("OfferStatusDescription")
                base["mtx_plan_charge"]         = offer_attr.get("TotalCharge")
                base["mtx_resource_id"]         = plan_offer.get("ResourceId")
                base["mtx_plan_activation_time"] = plan_offer.get("ActivationTime")
            else:
                base["mtx_plan_name"]   = "N/A"
                base["mtx_plan_code"]   = "N/A"
                base["mtx_plan_status"] = "N/A"
                base["mtx_plan_charge"] = "N/A"
                base["mtx_resource_id"] = "N/A"

        # Device check
        device_text = device_result.get("_resultText", "") or device_result.get("ResultText", "")
        if device_result.get("_resultText") == "OK":
            base["mtx_device_status"] = device_result.get("StatusDescription", "N/A")
        elif "not found" in device_text.lower():
            base["mtx_device_status"] = "Not Found"
        else:
            base["mtx_device_status"] = f"Error: {device_text}" if device_text else "N/A"

        # Validation
        review = []
        # BAN-CAN cross-check first — skip validation entirely if mismatched
        if order_ban and base["mtx_ban_can"] and base["mtx_ban_can"] != "N/A":
            if str(order_ban).strip() != str(base["mtx_ban_can"]).strip():
                base["mtx_field_to_review"] = "BANCAN_Mismatch"
                base["mtx_validation"]      = "✅"
                return base

        sub_status    = (base["mtx_subscription_status"] or "").lower()
        plan_status   = (base["mtx_plan_status"] or "").lower()
        device_status = (base["mtx_device_status"] or "").lower()

        if sub_status == "active":
            review.append("MTX_Subscription_Active")
        if plan_status == "active":
            # If plan activated after the disconnect order date → possible reactivation
            act_time = base.get("mtx_plan_activation_time")
            flagged_reactivation = False
            if activated_date and act_time:
                try:
                    from datetime import datetime, timezone
                    import re

                    def _to_utc(ts: str) -> datetime:
                        ts = ts.strip()
                        # Replace trailing Z with +00:00 for fromisoformat compatibility
                        ts = re.sub(r'Z$', '+00:00', ts)
                        # Python 3.7+ fromisoformat handles offsets like +0000 only with colon
                        ts = re.sub(r'([+-])(\d{2})(\d{2})$', r'\1\2:\3', ts)
                        # Remove sub-second precision beyond 6 digits
                        ts = re.sub(r'(\.\d{6})\d+', r'\1', ts)
                        try:
                            dt = datetime.fromisoformat(ts)
                        except ValueError:
                            # Fallback: strip timezone and assume UTC
                            dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                        # Normalise to UTC
                        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

                    act_dt = _to_utc(act_time)
                    ord_dt = _to_utc(activated_date)
                    if act_dt > ord_dt:
                        review.append("MTX_Possible_Reactivation")
                        flagged_reactivation = True
                except Exception:
                    pass
            if not flagged_reactivation:
                review.append("MTX_Plan_Active")
        if device_status == "active":
            review.append("MTX_Device_Status")

        if not review:
            review.append("No_Action_Required")

        base["mtx_field_to_review"] = "|".join(review)
        is_clean = "No_Action_Required" in review or "MTX_Possible_Reactivation" in review
        base["mtx_validation"]      = "✅" if is_clean else "⚠️"

        return base

    except requests.exceptions.RequestException as e:
        return {**base, "mtx_error": f"Request failed: {e}", "mtx_validation": "❌"}
    except Exception as e:
        return {**base, "mtx_error": str(e), "mtx_validation": "❌"}


def run_validation(msisdn_list: list[str]) -> list[dict]:
    return [process_single(m) for m in msisdn_list]
