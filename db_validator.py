"""
Order Validator — queries the dvsum database API for unbilled installment orders.
Uses a POST request with Bearer token authentication.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

DB_API_URL   = os.getenv("DB_API_URL",   "http://98.95.148.91/api/v1/execute")
DB_API_TOKEN = os.getenv("DB_API_TOKEN", "vgr_253c5970d0aef52119ab4a8a956b3863cfd35260264d8ced")
DB_TABLE     = '"dvsum-s3-glue-prod".vw_aria_extend_billed_unbilled_report'

HEADERS = {
    "Authorization": f"Bearer {DB_API_TOKEN}",
    "Content-Type":  "application/json",
}

ACCOUNTS_TABLE = '"dvsum-s3-glue-prod".vw_aria_accounts_raw'

TIMEOUT = 120


def _execute(sql: str) -> list[dict]:
    resp = requests.post(DB_API_URL, headers=HEADERS, json={"query": sql}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    rows    = data.get("rows", [])
    columns = [c.lower() for c in data.get("columns", [])]
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return [{k.lower(): v for k, v in r.items()} for r in rows]
    return [dict(zip(columns, row)) for row in rows]


def query_account_status(bancans: list[str]) -> dict[str, dict]:
    """
    Query vw_aria_accounts_raw for Account Status and MPI Status.
    Returns a dict keyed by bancan: {acct_status, mpi_status}.
    Batches in groups of 200 to avoid oversized IN clauses.
    """
    if not bancans:
        return {}

    result = {}
    batch_size = 200
    unique = list(set(bancans))

    for i in range(0, len(unique), batch_size):
        batch = unique[i:i + batch_size]
        in_list = ", ".join(f"'{b.replace(chr(39), chr(39)*2)}'" for b in batch)
        sql = (
            f"SELECT client_instance_id, acct_status, plan_status "
            f"FROM {ACCOUNTS_TABLE} "
            f"WHERE client_instance_id IN ({in_list}) "
            f"AND client_plan_id = 'Account_Master_Plan'"
        )
        rows = _execute(sql)
        for r in rows:
            result[r["client_instance_id"]] = {
                "acct_status": r.get("acct_status", ""),
                "mpi_status":  r.get("plan_status", ""),
            }

    return result


def query_orders(
    date_from:   str = None,
    date_to:     str = None,
    bancan:      str = None,
    msisdn:      str = None,
    plan_status: str = None,
) -> list[dict]:
    """
    Query the DB API for unbilled installment orders with optional filters.
    date_from / date_to: YYYY-MM-DD strings.
    Returns a list of row dicts.
    """
    conditions = [
        "ORDER_TYPE = 'Unbilled Orders'",
        "SKU LIKE '%Installment%'",
    ]

    if date_from and date_from.strip():
        conditions.append(f"date_parse(order_date, '%m/%d/%Y') >= date '{date_from.strip()}'")
    if date_to and date_to.strip():
        conditions.append(f"date_parse(order_date, '%m/%d/%Y') <= date '{date_to.strip()}'")
    if bancan and bancan.strip():
        safe = bancan.strip().replace("'", "''")
        conditions.append(f"bancan = '{safe}'")
    if msisdn and msisdn.strip():
        safe = msisdn.strip().replace("'", "''")
        conditions.append(f"msisdn = '{safe}'")
    if plan_status and plan_status.strip():
        safe = plan_status.strip().replace("'", "''")
        conditions.append(f"plan_status = '{safe}'")

    where = " AND ".join(conditions)
    sql = f"SELECT * FROM {DB_TABLE} WHERE {where} ORDER BY order_date DESC"
    return _execute(sql)
