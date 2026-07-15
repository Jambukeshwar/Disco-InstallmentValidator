import json
import os
import re
import requests
import concurrent.futures
import threading
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

ARIA_CLIENT_NO = int(os.getenv("ARIA_CLIENT_NO", "0"))
ARIA_AUTH_KEY = os.getenv("ARIA_AUTH_KEY", "")
ARIA_URL = os.getenv("ARIA_URL", "https://secure.ariasystems.net/api/AriaQuery/objects.php")
ARIA_URL_V1 = os.getenv("ARIA_URL_V1", "https://secure.ariasystems.net/v1/core")

HEADERS = {"Content-Type": "application/json"}


def plan_detailsfn(msisdn):
    payload = {
        "rest_call": "get_plan_instance_information_m",
        "releaseVersion": "52",
        "output_format": "json",
        "client_no": ARIA_CLIENT_NO,
        "auth_key": ARIA_AUTH_KEY,
        "limit": 100,
        "offset": 0,
        "query_string": f"product_field_value= {msisdn}"
    }
    response = requests.post(ARIA_URL, headers=HEADERS, data=json.dumps(payload))
    return response.json()


def child_planfn(user_id):
    payload = {
        "rest_call": "get_acct_plans_m",
        "output_format": "json",
        "client_no": ARIA_CLIENT_NO,
        "auth_key": ARIA_AUTH_KEY,
        "client_acct_id": user_id,
    }
    response = requests.post(ARIA_URL_V1, headers=HEADERS, data=json.dumps(payload))
    return response.json()['acct_plans_m']


def acct_statusfn(user_id):
    payload = {
        "rest_call": "get_acct_details_all_m",
        "output_format": "json",
        "client_no": ARIA_CLIENT_NO,
        "auth_key": ARIA_AUTH_KEY,
        "client_acct_id": user_id,
        "include_master_plans": "1",
        "include_supp_plans": "0",
        "include_billing_groups": "0",
        "include_payment_methods": "0",
        "plan_limit": "1",
    }
    response = requests.post(ARIA_URL_V1, headers=HEADERS, data=json.dumps(payload))
    return response.json()


def child_api(child_plan1_name):
    payload = {
        "rest_call": "get_plan_instance_information_m",
        "releaseVersion": "52",
        "output_format": "json",
        "client_no": ARIA_CLIENT_NO,
        "auth_key": ARIA_AUTH_KEY,
        "limit": 100,
        "offset": 0,
        "query_string": f"client_plan_instance_id={child_plan1_name}"
    }
    response = requests.post(ARIA_URL, headers=HEADERS, data=json.dumps(payload))
    result = response.json()
    final_result = result.get('plan_instance_details', [])
    string_val = 'N/A'
    for i in final_result:
        string_val = i['product_fields'][0]['product_field_value']
    return string_val


def nsodetails(child_plan1_name, user_id, string_details, msisdn):
    fields = string_details.split('|')
    InstallmentID = fields[5]
    Installremamt = fields[2]
    Installmentstatus = fields[9].strip() if fields[9] and fields[9].strip() != "" else "Blank"
    NSOExist = 'No'
    Installremamtcomp = int(float(Installremamt))

    OrderNo = Amount = OrderStatus = Invoice_No = Create_Date = Update_Date = NSOInstallmentIDs = 'N/A'
    AllIds = 'N/A'
    AllMSISDNs = 'N/A'

    NSO_NOT_REQUIRED_STATUSES = ('Full Pay Off of Installment', 'Buyers Remorse', 'Buyers Remorse - Delete Device', 'RAFM Cancellation', 'Death - Wearable', 'Death')

    if Installremamtcomp >= 1 and Installmentstatus not in NSO_NOT_REQUIRED_STATUSES:
        payload1 = {
            "rest_call": "get_order_m",
            "output_format": "json",
            "client_no": ARIA_CLIENT_NO,
            "auth_key": ARIA_AUTH_KEY,
            "client_acct_id": user_id
        }
        response1 = requests.post(ARIA_URL_V1, headers=HEADERS, data=json.dumps(payload1))
        response1.raise_for_status()
        result1 = response1.json()
        orders = result1.get("orders", [])

        for order in orders:
            if NSOExist == 'Yes':
                break
            orgOrderNo = order.get('order_no', 'N/A')
            Amounts = order.get("amount", "")

            new_payload = {
                "rest_call": "get_order_items_m",
                "output_format": "json",
                "client_no": ARIA_CLIENT_NO,
                "auth_key": ARIA_AUTH_KEY,
                "order_no": orgOrderNo
            }
            new_response = requests.post(ARIA_URL_V1, headers=HEADERS, data=json.dumps(new_payload))
            new_response.raise_for_status()
            new_result = new_response.json()

            for item in new_result.get("order_items_list", []):
                SKU = item.get('client_sku', 'N/A')
                line_comments = item.get('line_comments', 'N/A')
                if 'Installment_Balance' in SKU:
                    all_numbers = re.findall(r'\b\d+\b', line_comments)
                    filtered = [num for num in all_numbers if not (10 <= len(num) <= 11 or 1 <= len(num) <= 4)]
                    NSOInstallmentIDs = ' '.join(filtered)
                    modifiedamt = str(Amounts)
                    msisdnvalue = re.search(r'\|(\s*\d{10,11}\s*)\|', line_comments)
                    msisdn_comments = msisdnvalue.group(1).strip() if msisdnvalue else "NA"
                    AllIds += '|' + NSOInstallmentIDs
                    AllMSISDNs += '|' + msisdn_comments

                    if NSOInstallmentIDs == InstallmentID:
                        NSOExist = 'Yes'
                        OrderNo = order.get('order_no', 'N/A')
                        Amount = order.get("amount", "")
                        OrderStatus = order.get("status_label", "")
                        Invoice_No = order.get("invoice_no", "")
                        Create_Date = order.get("create_date", "")
                        Update_Date = order.get("update_date", "")
                    elif NSOInstallmentIDs == "" and round(float(modifiedamt), 2) == round(float(Installremamt), 2):
                        NSOExist = 'Same_Amt without_InstID'
                        OrderNo = order.get('order_no', 'N/A')
                        Amount = order.get("amount", "")
                        OrderStatus = order.get("status_label", "")
                        Invoice_No = order.get("invoice_no", "")
                        Create_Date = order.get("create_date", "")
                        Update_Date = order.get("update_date", "")
                    elif msisdn == msisdn_comments and round(float(modifiedamt), 2) == round(float(Installremamt), 2):
                        NSOExist = 'Same_Amt & MSISDN with Wrong_InstID'
                        OrderNo = order.get('order_no', 'N/A')
                        Amount = order.get("amount", "")
                        OrderStatus = order.get("status_label", "")
                        Invoice_No = order.get("invoice_no", "")
                        Create_Date = order.get("create_date", "")
                        Update_Date = order.get("update_date", "")
    else:
        NSOExist = 'NSO Not Required'

    return {
        "NSOExist": NSOExist,
        "Installmentstatus": Installmentstatus
    }


def _parse_date(date_str):
    """Safely parse a date string into a datetime object."""
    if not date_str:
        return datetime.min
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return datetime.min


def _select_best_device(device_plans: list) -> dict:
    """
    From a list of matching device plans, select the best one:
    - If any are Active → pick the most recent by plan_date (start date)
    - If all are Cancelled → pick the most recent by plan_instance_status_date
    """
    active_plans    = [cp for cp in device_plans if cp.get('plan_instance_status_label') == 'Active']
    cancelled_plans = [cp for cp in device_plans if cp.get('plan_instance_status_label') != 'Active']

    if active_plans:
        return max(active_plans, key=lambda cp: _parse_date(cp.get('plan_date')))
    else:
        return max(cancelled_plans, key=lambda cp: _parse_date(cp.get('plan_instance_status_date')))


def _validate_plan_instance(msisdn: str, plan: dict, activated_date: str = None, order_ban: str = None) -> dict:
    """Validate a single plan instance using its own BAN-CAN for all API calls."""
    try:
        user_id                 = plan.get('user_id')
        client_plan_instance_id = plan.get('client_plan_instance_id')
        plan_name               = plan.get('plan_name')
        status                  = plan.get('status_cd')
        plan_status_date        = plan.get('status_date')
        plan_status_cd          = 'Active' if status == 1 else 'Cancelled'

        # Each plan uses its own BAN-CAN — handles MSISDN migrated across accounts
        child_plan = child_planfn(user_id)

        masterplanstatus = 'N/A'
        No_of_inst = No_of_Active_inst = 0
        String_details = NSOExist = Installmentstatus = 'N/A'

        # ── Collect device plans linked to THIS plan instance ─────────────
        device_plans = []
        for cp in child_plan:
            if cp['client_plan_id'] == 'Account_Master_Plan':
                masterplanstatus = cp['plan_instance_status_label']

            client_parent_id = cp.get('client_parent_plan_instance_id')
            child_name       = cp.get('plan_name')

            if (client_parent_id == client_plan_instance_id
                    and child_name in ("Generic Device", "LLA General Device")):
                No_of_inst += 1
                if cp.get('plan_instance_status_label') == 'Active':
                    No_of_Active_inst += 1
                device_plans.append(cp)

        Child_plan1 = child_plan1_name = child_plan1_status = 'N/A'
        child_start_date = child_end_date = 'N/A'

        if device_plans:
            best               = _select_best_device(device_plans)
            Child_plan1        = best.get('plan_name')
            child_plan1_name   = best.get('client_plan_instance_id')
            child_plan1_status = best.get('plan_instance_status_label')
            child_start_date   = best.get('plan_date')
            child_end_date     = ('Not Applicable' if child_plan1_status == 'Active'
                                  else best.get('plan_instance_status_date'))

        # Account status from this plan's own BAN-CAN
        acct_result       = acct_statusfn(user_id)
        account_status_cd = acct_result['status_cd']
        account_status    = 'Active' if account_status_cd == 1 else ('Archived' if account_status_cd == -99 else 'Deactivated')

        if No_of_inst > 0:
            String_details    = child_api(child_plan1_name)
            nso               = nsodetails(child_plan1_name, user_id, String_details, msisdn)
            NSOExist          = nso['NSOExist']
            Installmentstatus = nso['Installmentstatus']

        remark = f'PlanStatus:{plan_status_cd}|DeviceStatus:{child_plan1_status}|StringStatus:{Installmentstatus}|NSO:{NSOExist}'

        reviewfield            = []
        plan_status_date_check = datetime.strptime(plan_status_date, "%Y-%m-%d %H:%M:%S")

        # ── Possible reactivation check ───────────────────────────────────
        possible_reactivation = False
        if plan_status_cd == 'Active':
            if activated_date:
                try:
                    from datetime import timezone as tz
                    act_dt = datetime.strptime(
                        activated_date.replace('+0000', '+00:00'),
                        "%Y-%m-%dT%H:%M:%S.%f%z"
                    ) if '.' in activated_date else datetime.strptime(
                        activated_date.replace('+0000', '+00:00'),
                        "%Y-%m-%dT%H:%M:%S%z"
                    )
                    aria_tz = tz(timedelta(hours=-5))
                    plan_dt = plan_status_date_check.replace(tzinfo=aria_tz)
                    if plan_dt > act_dt:
                        possible_reactivation = True
                except Exception:
                    if plan_status_date_check > datetime.now() - timedelta(days=5):
                        possible_reactivation = True
            else:
                if plan_status_date_check > datetime.now() - timedelta(days=3):
                    possible_reactivation = True

        base_result = {
            "msisdn":                   msisdn,
            "order_ban":                order_ban,
            "ban_can":                  user_id,
            "account_status":           account_status,
            "master_plan_status":       masterplanstatus,
            "client_plan_instance_id":  client_plan_instance_id,
            "plan_name":                plan_name,
            "plan_status":              plan_status_cd,
            "plan_status_date":         plan_status_date,
            "child_plan1":              Child_plan1,
            "child_client_plan1_id":    child_plan1_name,
            "child_plan1_status":       child_plan1_status,
            "child_start_date":         child_start_date,
            "child_end_date":           child_end_date,
            "inst_string":              String_details,
            "inst_status":              Installmentstatus,
            "no_of_inst":               No_of_inst,
            "no_of_active_inst":        No_of_Active_inst,
            "nso_exist":                NSOExist,
            "status_summary":           remark,
            "error_msg":                None,
        }

        if possible_reactivation:
            return {**base_result, "field_to_review": "Possible_reactivation", "validation": "✅"}

        # ── Normal flag checks ────────────────────────────────────────────

        child_end_older_3m = False
        if child_end_date and child_end_date not in ('N/A', 'Not Applicable'):
            for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
                try:
                    child_end_dt       = datetime.strptime(child_end_date, fmt)
                    child_end_older_3m = child_end_dt < datetime.now() - timedelta(days=90)
                    break
                except ValueError:
                    continue

        exempt_blank_inst = (
            Installmentstatus == 'Blank'
            and NSOExist == 'NSO Not Required'
            and child_plan1_status == 'Cancelled'
            and child_end_older_3m
        )

        if plan_status_cd != 'Cancelled':
            reviewfield.append('Plan_Status')
        if child_plan1_status == 'Active':
            reviewfield.append('Device_Status')
        if Installmentstatus in ('Blank', 'Active', 'active') and not exempt_blank_inst:
            reviewfield.append('Installment_Status')
        if NSOExist not in ('N/A', 'Yes', 'NSO Not Required'):
            reviewfield.append('NSO')
        if not reviewfield:
            reviewfield.append('No_Action_Required')

        validation = '✅' if 'No_Action_Required' in reviewfield else '⚠️'
        return {**base_result, "field_to_review": '|'.join(reviewfield), "validation": validation}

    except Exception as e:
        return {
            "msisdn":                  msisdn,
            "order_ban":               order_ban,
            "ban_can":                 plan.get('user_id'),
            "client_plan_instance_id": plan.get('client_plan_instance_id'),
            "plan_name":               plan.get('plan_name'),
            "error_msg":               str(e),
        }


def process_single(msisdn: str, activated_date: str = None, order_ban: str = None) -> list[dict]:
    """Process one MSISDN — returns one result dict per plan instance found in Aria."""
    try:
        plan_details = plan_detailsfn(msisdn)

        if plan_details.get('error_msg') != 'OK' or plan_details.get('total_records', 0) == 0:
            error = plan_details.get('error_msg', 'No Data Found')
            return [{"msisdn": msisdn, "order_ban": order_ban, "error_msg": error}]

        results = []
        for plan in plan_details['plan_instance_details']:
            results.append(_validate_plan_instance(msisdn, plan, activated_date, order_ban))

        return results

    except Exception as e:
        return [{"msisdn": msisdn, "order_ban": order_ban, "error_msg": str(e)}]


def run_validation(msisdn_list: list[str], max_workers: int = 3) -> list[dict]:
    """Run validation for a list of MSISDNs concurrently. Returns one dict per plan instance."""
    results = []
    lock = threading.Lock()

    def process_and_collect(msisdn):
        plan_results = process_single(msisdn)  # list[dict]
        with lock:
            results.extend(plan_results)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(process_and_collect, msisdn_list))

    return results