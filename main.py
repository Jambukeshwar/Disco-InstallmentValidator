import io
import os
import requests
import threading
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

from database import init_db, get_connection
from salesforce import build_login_url, handle_callback, is_authenticated, clear_token, run_soql, SalesforceError
from aria_validator import run_validation, acct_statusfn, child_planfn, ARIA_CLIENT_NO, ARIA_AUTH_KEY, ARIA_URL_V1, HEADERS
from aria_actions import cancel_plan, cancel_installment, create_nso_order, activate_plan
from matrixx_actions import deactivate_device
import matrixx_validator
import sf_asset_validator
from auth import init_auth_db, verify_login, logout as auth_logout, get_session
from email_report import build_html_body, send_report, build_connectivity_alert_html, send_alert
import installment_validator as inst_val
from salesforce import run_soql_query
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

load_dotenv()

app = FastAPI(title="Disco Validator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()
init_auth_db()

_scheduler = BackgroundScheduler(timezone="UTC")

# Reset any runs left in "running" state from a previous crashed/restarted process
_startup_conn = get_connection()
_startup_conn.execute("UPDATE runs SET status='interrupted' WHERE status='running'")
_startup_conn.execute("UPDATE order_runs SET status='interrupted' WHERE status IN ('running','stopping')")
_startup_conn.commit()
_startup_conn.close()


# ── Pydantic models ────────────────────────────────────────────────────────────

class InstallmentRunRequest(BaseModel):
    segment:   str
    date_from: str
    time_from: str = "00:00"
    date_to:   str
    time_to:   str = "23:59"




class SchedulerConfigRequest(BaseModel):
    enabled:              bool
    trigger_hour:         int
    trigger_minute:       int
    report_range:         str
    subject_prefix:       str
    inst_enabled:         bool
    inst_trigger_hour:    int
    inst_trigger_minute:  int
    inst_report_range:    str
    inst_subject_prefix:  str
    smtp_to:              str
    smtp_sender_name:     str


class RunRequest(BaseModel):
    date_from: str
    time_from: str = "00:00"
    date_to: str
    time_to: str = "23:59"

class ManualRunRequest(BaseModel):
    msisdns: list[str]

class OrderValidatorRequest(BaseModel):
    date_from:   str = ""
    date_to:     str = ""
    bancan:      str = ""
    msisdn:      str = ""
    plan_status: str = ""

class LoginRequest(BaseModel):
    username: str
    password: str

class MatrixxKeysRequest(BaseModel):
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_session_token: str

class SFTokenRequest(BaseModel):
    refresh_token: str
    access_token: str
    instance_url: str

class MatrixxDeactivateRequest(BaseModel):
    msisdn: str
    channel: str

class ActivatePlanRequest(BaseModel):
    ban_can: str
    plan_instance_id: str
    caller_name: str

class CancelPlanRequest(BaseModel):
    ban_can: str
    plan_instance_id: str
    caller_name: str

class CancelInstallmentRequest(BaseModel):
    ban_can: str
    child_plan_instance_id: str
    order_reason: str
    caller_name: str

class CreateNSORequest(BaseModel):
    ban_can: str
    child_plan_instance_id: str
    sku: str
    amount: float
    msisdn: str
    installment_id: str


# ── Session guard ──────────────────────────────────────────────────────────────

def require_session(request: Request):
    token = request.cookies.get("disco_session")
    session = get_session(token)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return session


# ── App login/logout endpoints ─────────────────────────────────────────────────

@app.post("/api/login")
def app_login(body: LoginRequest, response: Response):
    token = verify_login(body.username, body.password)
    if not token:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    response.set_cookie(
        key="disco_session",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=3600
    )
    return {"status": "ok", "username": body.username}


@app.post("/api/logout")
def app_logout(request: Request, response: Response):
    token = request.cookies.get("disco_session")
    if token:
        auth_logout(token)
    response.delete_cookie("disco_session")
    return {"status": "logged_out"}


@app.get("/api/me")
def get_me(request: Request):
    token = request.cookies.get("disco_session")
    session = get_session(token)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"username": session["username"]}


# ── SF Auth endpoints ──────────────────────────────────────────────────────────

@app.get("/api/auth/status")
def auth_status():
    if not is_authenticated():
        return {"authenticated": False}
    try:
        from salesforce import test_connection
        test_connection()
        return {"authenticated": True}
    except Exception:
        return {"authenticated": False}


@app.get("/api/auth/login")
def auth_login():
    url = build_login_url()
    return RedirectResponse(url=url)


@app.get("/api/auth/callback")
def auth_callback(code: str, state: str):
    try:
        handle_callback(code, state)
    except SalesforceError as e:
        return HTMLResponse(f"<h3>Login failed</h3><p>{e}</p>", status_code=400)
    return RedirectResponse(url="/", status_code=302)


@app.post("/api/auth/logout")
def sf_auth_logout():
    clear_token()
    return {"status": "logged_out"}


# ── Matrixx Actions ────────────────────────────────────────────────────────────

@app.post("/api/actions/matrixx-deactivate")
def action_matrixx_deactivate(req: MatrixxDeactivateRequest, request: Request):
    require_session(request)
    try:
        result = deactivate_device(req.msisdn, req.channel)
        return {"status": "ok", "matrixx_response": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Aria Actions ───────────────────────────────────────────────────────────────

@app.post("/api/actions/activate-plan")
def action_activate_plan(req: ActivatePlanRequest, request: Request):
    require_session(request)
    try:
        result = activate_plan(req.ban_can, req.plan_instance_id, req.caller_name)
        return {"status": "ok", "aria_response": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/actions/cancel-plan")
def action_cancel_plan(req: CancelPlanRequest, request: Request):
    require_session(request)
    try:
        result = cancel_plan(req.ban_can, req.plan_instance_id, req.caller_name)
        return {"status": "ok", "aria_response": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/actions/cancel-installment")
def action_cancel_installment(req: CancelInstallmentRequest, request: Request):
    require_session(request)
    try:
        result = cancel_installment(
            req.ban_can,
            req.child_plan_instance_id,
            req.order_reason,
            req.caller_name,
        )
        return {"status": "ok", "aria_response": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/actions/create-nso")
def action_create_nso(req: CreateNSORequest, request: Request):
    require_session(request)
    try:
        result = create_nso_order(
            req.ban_can,
            req.child_plan_instance_id,
            req.sku,
            req.amount,
            req.msisdn,
            req.installment_id,
        )
        return {"status": "ok", "aria_response": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Order Validator ───────────────────────────────────────────────────────────

import db_validator as _db_validator


def _order_run_is_stopping(conn, run_id: int) -> bool:
    row = conn.execute("SELECT status FROM order_runs WHERE id=?", (run_id,)).fetchone()
    return row and row["status"] == "stopping"


def _process_order_validator(run_id: int, filters: dict):
    conn = get_connection()
    try:
        # 1. Fetch orders
        conn.execute("UPDATE order_runs SET message=? WHERE id=?", ("Step 1/2: Querying orders from database (this may take 30–90s)…", run_id))
        conn.commit()
        rows = _db_validator.query_orders(**filters)

        if _order_run_is_stopping(conn, run_id):
            conn.execute("UPDATE order_runs SET status='stopped', message='Stopped by user.' WHERE id=?", (run_id,))
            conn.commit()
            return

        total = len(rows)
        conn.execute("UPDATE order_runs SET total=?, message=? WHERE id=?",
                     (total, f"Step 1/2 done — {total} orders found. Step 2/2: Fetching account status…", run_id))
        conn.commit()

        # 2. Fetch Account Status + MPI Status for all unique BANCANs
        bancans = list({r.get("bancan","") for r in rows if r.get("bancan","")})
        try:
            acct_lookup = _db_validator.query_account_status(bancans) if bancans else {}
        except Exception:
            acct_lookup = {}

        if _order_run_is_stopping(conn, run_id):
            conn.execute("UPDATE order_runs SET status='stopped', message='Stopped by user.' WHERE id=?", (run_id,))
            conn.commit()
            return

        conn.execute("UPDATE order_runs SET message=? WHERE id=?", ("Step 2/2 done — merging results…", run_id))
        conn.commit()

        # 3. Insert merged rows
        for row in rows:
            acct_info = acct_lookup.get(row.get("bancan",""), {})
            conn.execute("""
                INSERT INTO order_results
                    (run_id, order_type, bancan, acct_no, master_plan_instance_no,
                     sku, order_date, order_no, quantity, unit_price, amount,
                     order_line_comment, product, msisdn, installment_id,
                     plan_description, client_instance_id, aria_instance_no, plan_status,
                     acct_status, mpi_status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (run_id,
                  row.get("order_type",""), row.get("bancan",""), row.get("acct_no",""),
                  row.get("master_plan_instance_no",""), row.get("sku",""),
                  row.get("order_date",""), row.get("order_no",""), row.get("quantity",""),
                  row.get("unit_price",""), row.get("amount",""),
                  row.get("order_line_comment",""), row.get("product",""),
                  row.get("msisdn",""), row.get("installment_id",""),
                  row.get("plan_description",""), row.get("client_instance_id",""),
                  row.get("aria_instance_no",""), row.get("plan_status",""),
                  acct_info.get("acct_status",""), acct_info.get("mpi_status","")))
        conn.execute("UPDATE order_runs SET status='completed' WHERE id=?", (run_id,))
        conn.commit()
    except Exception as e:
        conn.execute("UPDATE order_runs SET status='failed', error=? WHERE id=?", (str(e), run_id))
        conn.commit()
    finally:
        conn.close()


def _order_results_from_db(run_id: int) -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM order_results WHERE run_id=? ORDER BY order_date DESC, bancan", (run_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/order-validator/run")
def start_order_validator(req: OrderValidatorRequest, background_tasks: BackgroundTasks, request: Request):
    require_session(request)
    import json as _json
    filters = {
        "date_from":   req.date_from   or None,
        "date_to":     req.date_to     or None,
        "bancan":      req.bancan      or None,
        "msisdn":      req.msisdn      or None,
        "plan_status": req.plan_status or None,
    }
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO order_runs (triggered_at, status, filters, total) VALUES (?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), "running", _json.dumps(filters), 0)
    )
    run_id = cur.lastrowid
    old = conn.execute(
        "SELECT id FROM order_runs ORDER BY triggered_at DESC LIMIT -1 OFFSET 10"
    ).fetchall()
    for row in old:
        conn.execute("DELETE FROM order_results WHERE run_id=?", (row["id"],))
        conn.execute("DELETE FROM order_runs WHERE id=?", (row["id"],))
    conn.commit()
    conn.close()
    background_tasks.add_task(_process_order_validator, run_id, filters)
    return {"run_id": run_id}


@app.get("/api/order-validator/runs")
def list_order_runs(request: Request):
    require_session(request)
    conn = get_connection()
    rows = conn.execute("SELECT * FROM order_runs ORDER BY triggered_at DESC LIMIT 10").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/order-validator/run/{run_id}/stop")
def stop_order_run(run_id: int, request: Request):
    require_session(request)
    conn = get_connection()
    run = conn.execute("SELECT status FROM order_runs WHERE id=?", (run_id,)).fetchone()
    if run and run["status"] == "running":
        conn.execute("UPDATE order_runs SET status='stopping', message='Stop requested — waiting for current query to finish…' WHERE id=?", (run_id,))
        conn.commit()
    conn.close()
    return {"status": "stop_requested"}


@app.delete("/api/order-validator/run/{run_id}")
def delete_order_run(run_id: int, request: Request):
    require_session(request)
    conn = get_connection()
    run = conn.execute("SELECT status FROM order_runs WHERE id=?", (run_id,)).fetchone()
    if not run:
        conn.close()
        raise HTTPException(status_code=404, detail="Run not found")
    if run["status"] in ("running", "stopping"):
        conn.close()
        raise HTTPException(status_code=409, detail="Cannot delete a run that is still in progress")
    conn.execute("DELETE FROM order_results WHERE run_id=?", (run_id,))
    conn.execute("DELETE FROM order_runs WHERE id=?", (run_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}


@app.get("/api/order-validator/run/{run_id}")
def get_order_run(run_id: int, request: Request):
    require_session(request)
    conn = get_connection()
    run = conn.execute("SELECT * FROM order_runs WHERE id=?", (run_id,)).fetchone()
    conn.close()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    results = _order_results_from_db(run_id) if run["status"] == "completed" else []
    return {"id": run["id"], "status": run["status"], "total": run["total"],
            "error": run["error"], "message": run["message"], "results": results}


@app.get("/api/order-validator/run/{run_id}/download")
def download_order_validator(run_id: int, request: Request):
    require_session(request)
    conn = get_connection()
    run = conn.execute("SELECT * FROM order_runs WHERE id=?", (run_id,)).fetchone()
    conn.close()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    results = _order_results_from_db(run_id)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Order Validator"

    hdr_fill  = PatternFill("solid", fgColor="1F4E79")
    green     = PatternFill("solid", fgColor="C6EFCE")
    amber     = PatternFill("solid", fgColor="FFEB9C")
    red_fill  = PatternFill("solid", fgColor="FFC7CE")

    headers = ["Flag", "BAN-CAN", "Account Status", "MPI Status", "Order Type",
               "MSISDN", "Order No", "Order Date", "SKU", "Product", "Amount",
               "Installment ID", "Plan Status", "Plan Description",
               "Client Instance ID", "Aria Instance No", "Master Plan Instance No",
               "Order Line Comment", "Acct No"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = hdr_fill
        cell.alignment = Alignment(horizontal="center")

    for r in results:
        plan_status  = (r.get("plan_status")  or "").strip()
        mpi_status   = (r.get("mpi_status")   or "").strip().lower()
        order_type   = (r.get("order_type")   or "").strip().lower()
        is_unbilled  = "unbilled" in order_type
        is_suspended = mpi_status == "suspended"
        flag = "🔴" if (is_unbilled and is_suspended) else "🟢"
        fill = red_fill if (is_unbilled and is_suspended) else green
        row = [flag, r.get("bancan",""), r.get("acct_status",""), r.get("mpi_status",""),
               r.get("order_type",""), r.get("msisdn",""), r.get("order_no",""),
               r.get("order_date",""), r.get("sku",""), r.get("product",""),
               r.get("amount",""), r.get("installment_id",""), plan_status,
               r.get("plan_description",""), r.get("client_instance_id",""),
               r.get("aria_instance_no",""), r.get("master_plan_instance_no",""),
               r.get("order_line_comment",""), r.get("acct_no","")]
        ws.append(row)
        if fill:
            for cell in ws[ws.max_row]:
                cell.fill = fill

    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 60)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"order_validator_{run_id}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# ── Legacy stub — kept so old NSO validator references don't break ─────────────

def _check_nso_for_bancan(ban_can: str, msisdn: str) -> dict:
    import json as _json
    base = {"ban_can": ban_can, "msisdn": msisdn, "account_status": "N/A",
            "master_plan_status": "N/A", "last_bill_date": "", "status_date": "",
            "nso_orders": [], "nso_count": 0, "error": None}
    try:
        # Single API call — get_acct_details_all_m returns account status, master plan status, dates
        acct = acct_statusfn(ban_can)
        status_cd = acct.get("status_cd")
        base["account_status"] = "Active" if status_cd == 1 else ("Archived" if status_cd == -99 else "Deactivated")

        mp_list = acct.get("master_plans_info") or []
        if mp_list:
            base["master_plan_status"] = mp_list[0].get("mp_instance_status_label", "N/A")
            base["last_bill_date"]     = mp_list[0].get("last_bill_date", "")
            base["status_date"]        = mp_list[0].get("status_date", "")

        # NSO orders
        order_resp = requests.post(ARIA_URL_V1, headers=HEADERS, data=_json.dumps({
            "rest_call": "get_order_m", "output_format": "json",
            "client_no": ARIA_CLIENT_NO, "auth_key": ARIA_AUTH_KEY, "client_acct_id": ban_can
        }))
        nso_orders = []
        for order in order_resp.json().get("orders", []):
            order_no = order.get("order_no", "N/A")
            items_resp = requests.post(ARIA_URL_V1, headers=HEADERS, data=_json.dumps({
                "rest_call": "get_order_items_m", "output_format": "json",
                "client_no": ARIA_CLIENT_NO, "auth_key": ARIA_AUTH_KEY, "order_no": order_no
            }))
            for item in items_resp.json().get("order_items_list", []):
                if "Installment_Balance" in item.get("client_sku", ""):
                    nso_orders.append({
                        "order_no":     order_no,
                        "sku":          item.get("client_sku", ""),
                        "amount":       order.get("amount", ""),
                        "status":       order.get("status_label", ""),
                        "invoice_no":   order.get("invoice_no", ""),
                        "create_date":  order.get("create_date", ""),
                        "update_date":  order.get("update_date", ""),
                        "line_comments": item.get("line_comments", ""),
                    })
        base["nso_orders"] = nso_orders
        base["nso_count"]  = len(nso_orders)
    except Exception as e:
        base["error"] = str(e)
    return base




# ── AI Suggestion ─────────────────────────────────────────────────────────────

def generate_suggestion(aria_fields: str, mtx_fields: str) -> str:
    """Generate a prioritized next-action suggestion based on Aria and Matrixx field_to_review values."""
    aria = set((aria_fields or "").split("|"))
    mtx  = set((mtx_fields  or "").split("|"))

    steps = []

    # BAN-CAN mismatch — stop here, no corrective actions suggested
    if "BANCAN_Mismatch" in aria and "BANCAN_Mismatch" in mtx:
        return "BAN-CAN mismatch in both Aria and Matrixx — verify order BAN-CAN before taking any action"
    if "BANCAN_Mismatch" in aria:
        return "BAN-CAN mismatch in Aria — verify order BAN-CAN before taking any action"
    if "BANCAN_Mismatch" in mtx:
        return "BAN-CAN mismatch in Matrixx — verify order BAN-CAN before taking any action"

    # Only suggest actions if BAN-CAN matches
    # Aria actions — follow hierarchy: NSO → Cancel device with reason → Cancel plan
    if "Possible_reactivation" in aria:
        steps.append("Verify if customer was reactivated — no action may be needed")

    plan_cancelled = "Plan_Status" not in aria   # Plan_Status flag means plan is NOT cancelled
    device_active  = "Device_Status" in aria
    nso_needed     = "NSO" in aria
    inst_review    = "Installment_Status" in aria

    if plan_cancelled and (nso_needed or device_active):
        # Plan is cancelled but still has pending work — must activate plan first
        steps.append("Activate plan in Aria")
        if nso_needed:
            steps.append("Create NSO balance order in Aria")
        if device_active:
            steps.append("Cancel device with reason in Aria")
        steps.append("Cancel plan in Aria")
    else:
        # Normal hierarchy: NSO → Cancel device with reason → Cancel plan
        if nso_needed:
            steps.append("Create NSO balance order in Aria")
        if device_active:
            steps.append("Cancel device with reason in Aria")
        if "Plan_Status" in aria:
            steps.append("Cancel plan in Aria")

    if inst_review and not nso_needed:
        steps.append("Review installment status in Aria")

    # Matrixx actions
    if "Device_Active" in mtx:
        steps.append("Deactivate device in Matrixx")
    if "Subscription_Active" in mtx:
        steps.append("Check active subscription in Matrixx")
    if "Plan_Active" in mtx:
        steps.append("Check active plan in Matrixx")

    all_clean = (
        "No_Action_Required" in aria and
        "No_Action_Required" in mtx
    )

    if all_clean:
        return "All systems clean — no action required."

    if not steps:
        return "Review flagged fields manually."

    return " → ".join(steps)


# ── Runs ───────────────────────────────────────────────────────────────────────

def trim_old_runs(conn, keep: int = 8):
    old = conn.execute(
        "SELECT id FROM runs ORDER BY triggered_at DESC LIMIT -1 OFFSET ?", (keep,)
    ).fetchall()
    for row in old:
        conn.execute("DELETE FROM results WHERE run_id = ?", (row["id"],))
        conn.execute("DELETE FROM matrixx_results WHERE run_id = ?", (row["id"],))
        conn.execute("DELETE FROM sf_asset_results WHERE run_id = ?", (row["id"],))
        conn.execute("DELETE FROM runs WHERE id = ?", (row["id"],))
    if old:
        conn.commit()


def trim_old_installment_runs(conn, keep: int = 15):
    old = conn.execute(
        "SELECT id FROM installment_runs ORDER BY triggered_at DESC LIMIT -1 OFFSET ?", (keep,)
    ).fetchall()
    for row in old:
        conn.execute("DELETE FROM installment_results WHERE run_id=?", (row["id"],))
        conn.execute("DELETE FROM installment_runs WHERE id=?", (row["id"],))
    if old:
        conn.commit()



def _insert_result(conn, run_id: int, r: dict):
    conn.execute("""
        INSERT INTO results (
            run_id, msisdn, order_ban, ban_can, account_status, master_plan_status,
            client_plan_instance_id, plan_name, plan_status, plan_status_date,
            child_plan1, child_client_plan1_id, child_plan1_status,
            child_start_date, child_end_date, inst_string, inst_status,
            no_of_inst, no_of_active_inst, nso_exist, status_summary,
            field_to_review, validation, error_msg
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        run_id,
        r.get("msisdn"), r.get("order_ban"), r.get("ban_can"),
        r.get("account_status"), r.get("master_plan_status"),
        r.get("client_plan_instance_id"), r.get("plan_name"),
        r.get("plan_status"), r.get("plan_status_date"),
        r.get("child_plan1"), r.get("child_client_plan1_id"),
        r.get("child_plan1_status"), r.get("child_start_date"),
        r.get("child_end_date"), r.get("inst_string"), r.get("inst_status"),
        r.get("no_of_inst"), r.get("no_of_active_inst"), r.get("nso_exist"),
        r.get("status_summary"), r.get("field_to_review"),
        r.get("validation"), r.get("error_msg")
    ))


def _insert_matrixx_result(conn, run_id: int, r: dict):
    conn.execute("""
        INSERT INTO matrixx_results (
            run_id, msisdn, mtx_order_ban, mtx_ban_can, mtx_segment, mtx_payment_pref,
            mtx_usage_guide, mtx_subscription, mtx_subscription_id,
            mtx_subscription_status, mtx_plan_name, mtx_plan_code,
            mtx_plan_status, mtx_plan_charge, mtx_resource_id,
            mtx_validation, mtx_field_to_review, mtx_error
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        run_id,
        r.get("msisdn"), r.get("mtx_order_ban"), r.get("mtx_ban_can"),
        r.get("mtx_segment"), r.get("mtx_payment_pref"), r.get("mtx_usage_guide"),
        r.get("mtx_subscription"), r.get("mtx_subscription_id"),
        r.get("mtx_subscription_status"), r.get("mtx_plan_name"),
        r.get("mtx_plan_code"), r.get("mtx_plan_status"),
        r.get("mtx_plan_charge"), r.get("mtx_resource_id"),
        r.get("mtx_validation"), r.get("mtx_field_to_review"),
        r.get("mtx_error")
    ))


def _insert_sf_asset_result(conn, run_id: int, r: dict):
    conn.execute("""
        INSERT INTO sf_asset_results (
            run_id, msisdn, sf_asset_id, sf_ban_can, sf_provisioning,
            sf_disc_reason, sf_product, sf_asset_ref_id, sf_root_item_id,
            sf_last_modified, sf_validation, sf_field_to_review, sf_error
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        run_id,
        r.get("msisdn"), r.get("sf_asset_id"), r.get("sf_ban_can"),
        r.get("sf_provisioning"), r.get("sf_disc_reason"), r.get("sf_product"),
        r.get("sf_asset_ref_id"), r.get("sf_root_item_id"), r.get("sf_last_modified"),
        r.get("sf_validation"), r.get("sf_field_to_review"), r.get("sf_error")
    ))


def _process_and_store(run_id: int, msisdn: str, counters: dict, lock, order_ban: str = None, order_reason: str = None, activated_date: str = None) -> None:
    from aria_validator import process_single as aria_single
    aria_r = aria_single(msisdn, activated_date=activated_date, order_ban=order_ban)
    mtx_r  = matrixx_validator.process_single(msisdn, order_ban=order_ban)
    sf_r   = sf_asset_validator.process_single(msisdn, order_ban=order_ban, order_reason=order_reason)

    conn = get_connection()
    try:
        _insert_result(conn, run_id, aria_r)
        _insert_matrixx_result(conn, run_id, mtx_r)
        _insert_sf_asset_result(conn, run_id, sf_r)

        with lock:
            if aria_r.get("error_msg"):
                counters["errors"] += 1
            elif aria_r.get("validation") == "⚠️":
                counters["warns"] += 1
            else:
                counters["success"] += 1
            s = counters["success"]
            w = counters["warns"]
            e = counters["errors"]

        conn.execute(
            "UPDATE runs SET success_count = ?, warn_count = ?, error_count = ? WHERE id = ?",
            (s, w, e, run_id)
        )
        conn.commit()
    finally:
        conn.close()


def _run_concurrent(run_id: int, msisdn_list: list, max_workers: int = 3, order_meta: dict = None):
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    counters = {"success": 0, "warns": 0, "errors": 0}
    lock = threading.Lock()
    meta = order_meta or {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _process_and_store, run_id, msisdn, counters, lock,
                meta.get(msisdn, {}).get("ban"),
                meta.get(msisdn, {}).get("reason"),
                meta.get(msisdn, {}).get("activated_date")
            ): msisdn
            for msisdn in msisdn_list
        }
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"Worker error for {futures[future]}: {e}")
            if _stop_flags.get(run_id):
                for f in futures:
                    f.cancel()
                break

    _stop_flags.pop(run_id, None)
    return counters


def do_run(run_id: int, date_from: str, time_from: str, date_to: str, time_to: str):
    conn = get_connection()
    try:
        sf_records = run_soql(date_from, time_from, date_to, time_to)
        order_meta = {}
        for r in sf_records:
            m = r.get("PR_MSISDN__c")
            if m:
                billing        = r.get("vlocity_cmt__BillingAccountId__r") or {}
                ban            = billing.get("PR_Mobile_Billing_Number__c") if isinstance(billing, dict) else None
                order          = r.get("order") or {}
                reason         = order.get("vlocity_cmt__Reason__c") if isinstance(order, dict) else None
                activated_date = order.get("ActivatedDate") if isinstance(order, dict) else None
                order_meta[m]  = {"ban": ban, "reason": reason, "activated_date": activated_date}

        msisdn_list = list(order_meta.keys())
        conn.execute("UPDATE runs SET total_count = ? WHERE id = ?", (len(msisdn_list), run_id))
        conn.commit()

        if not msisdn_list:
            conn.execute(
                "UPDATE runs SET status = 'completed', finished_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), run_id)
            )
            conn.commit()
            return

        counters = _run_concurrent(run_id, msisdn_list, max_workers=3, order_meta=order_meta)
        success, warns, errors = counters["success"], counters["warns"], counters["errors"]

        final_status = 'stopped' if _stop_flags.get(run_id) else 'completed'
        conn.execute("""
            UPDATE runs SET status = ?, finished_at = ?,
            success_count = ?, warn_count = ?, error_count = ? WHERE id = ?
        """, (final_status, datetime.now(timezone.utc).isoformat(), success, warns, errors, run_id))
        conn.commit()

    except SalesforceError as e:
        status = "auth_required" if "NOT_AUTHENTICATED" in str(e) else "failed"
        conn.execute(
            "UPDATE runs SET status = ?, finished_at = ? WHERE id = ?",
            (status, datetime.now(timezone.utc).isoformat(), run_id)
        )
        conn.commit()
        print(f"Run {run_id} Salesforce error: {e}")
    except Exception as e:
        conn.execute(
            "UPDATE runs SET status = 'failed', finished_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), run_id)
        )
        conn.commit()
        print(f"Run {run_id} failed: {e}")
    finally:
        trim_old_runs(conn)
        conn.close()


def do_manual_run(run_id: int, msisdn_list: list[str]):
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE runs SET total_count = ? WHERE id = ?",
            (len(msisdn_list), run_id)
        )
        conn.commit()

        counters = _run_concurrent(run_id, msisdn_list, max_workers=3, order_meta={})
        success, warns, errors = counters["success"], counters["warns"], counters["errors"]

        final_status = 'stopped' if _stop_flags.get(run_id) else 'completed'
        conn.execute("""
            UPDATE runs SET status = ?, finished_at = ?,
            success_count = ?, warn_count = ?, error_count = ? WHERE id = ?
        """, (final_status, datetime.now(timezone.utc).isoformat(), success, warns, errors, run_id))
        conn.commit()

    except Exception as e:
        conn.execute(
            "UPDATE runs SET status = 'failed', finished_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), run_id)
        )
        conn.commit()
        print(f"Manual run {run_id} failed: {e}")
    finally:
        trim_old_runs(conn)
        conn.close()


@app.post("/api/runs/manual")
def create_manual_run(req: ManualRunRequest, background_tasks: BackgroundTasks, request: Request):
    require_session(request)
    msisdn_list = list({m.strip() for m in req.msisdns if m.strip()})
    if not msisdn_list:
        raise HTTPException(status_code=400, detail="No valid MSISDNs provided")

    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO runs (triggered_at, date_from, date_to, status) VALUES (?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), "manual", "manual", "running")
    )
    run_id = cur.lastrowid
    conn.commit()
    conn.close()

    background_tasks.add_task(do_manual_run, run_id, msisdn_list)
    return {"run_id": run_id, "status": "running"}


@app.post("/api/runs")
def create_run(req: RunRequest, background_tasks: BackgroundTasks, request: Request):
    require_session(request)
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO runs (triggered_at, date_from, time_from, date_to, time_to, status) VALUES (?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), req.date_from, req.time_from, req.date_to, req.time_to, "running")
    )
    run_id = cur.lastrowid
    conn.commit()
    conn.close()

    background_tasks.add_task(do_run, run_id, req.date_from, req.time_from, req.date_to, req.time_to)
    return {"run_id": run_id, "status": "running"}


@app.delete("/api/runs")
def clear_runs(request: Request):
    require_session(request)
    conn = get_connection()
    conn.execute("DELETE FROM results")
    conn.execute("DELETE FROM matrixx_results")
    conn.execute("DELETE FROM sf_asset_results")
    conn.execute("DELETE FROM runs")
    conn.commit()
    conn.close()
    return {"status": "cleared"}


@app.get("/api/runs")
def list_runs(request: Request):
    require_session(request)
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY triggered_at DESC LIMIT 5"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/runs/{run_id}")
def get_run(run_id: int, request: Request):
    require_session(request)
    conn = get_connection()
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    results = conn.execute(
        "SELECT * FROM results WHERE run_id = ? ORDER BY msisdn", (run_id,)
    ).fetchall()
    conn.close()
    return {"run": dict(run), "results": [dict(r) for r in results]}


@app.get("/api/runs/{run_id}/status")
def get_run_status(run_id: int, request: Request):
    require_session(request)
    conn = get_connection()
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return dict(run)


def _build_excel_bytes(run_id: int):
    """Build Excel report for a run and return (bytes, filename) tuple."""
    conn = get_connection()
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        raise ValueError(f"Run {run_id} not found")
    aria_results = conn.execute("SELECT * FROM results          WHERE run_id = ? ORDER BY msisdn", (run_id,)).fetchall()
    mtx_results  = conn.execute("SELECT * FROM matrixx_results  WHERE run_id = ? ORDER BY msisdn", (run_id,)).fetchall()
    sf_results   = conn.execute("SELECT * FROM sf_asset_results WHERE run_id = ? ORDER BY msisdn", (run_id,)).fetchall()
    conn.close()
    buf, filename = _build_excel_workbook(run, aria_results, mtx_results, sf_results)
    return buf.getvalue(), filename


def _build_excel_workbook(run, aria_results, mtx_results, sf_results):
    """Build the Excel workbook and return (BytesIO, filename)."""
    run_d = dict(run)

    header_fill = PatternFill("solid", fgColor="1E3A5F")
    header_font = Font(bold=True, color="FFFFFF")

    def style_headers(ws, headers):
        for col, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=h)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")

    def color_rows(ws, results, val_key, headers):
        for row_idx, r in enumerate(results, 2):
            v = r.get(val_key, "")
            if v == "✅":   fill = PatternFill("solid", fgColor="E8F5E9")
            elif v == "⚠️": fill = PatternFill("solid", fgColor="FFF8E1")
            else:            fill = PatternFill("solid", fgColor="FFEBEE")
            for col in range(1, len(headers) + 1):
                ws.cell(row=row_idx, column=col).fill = fill

    def autofit(ws):
        for col in ws.columns:
            max_len = max((len(str(cell.value or "")) for cell in col), default=10)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)

    wb = openpyxl.Workbook()

    # ── Sheet 1: Summary
    ws0 = wb.active
    ws0.title = "Summary"

    title_font   = Font(bold=True, size=13, color="FFFFFF")
    title_fill   = PatternFill("solid", fgColor="1E3A5F")
    section_font = Font(bold=True, size=11, color="1E3A5F")
    label_font   = Font(bold=True)
    green_fill   = PatternFill("solid", fgColor="E8F5E9")
    amber_fill   = PatternFill("solid", fgColor="FFF8E1")
    red_fill     = PatternFill("solid", fgColor="FFEBEE")
    grey_fill    = PatternFill("solid", fgColor="F5F5F5")

    def s_cell(ws, row, col, value, font=None, fill=None, align="left"):
        c = ws.cell(row=row, column=col, value=value)
        if font:  c.font = font
        if fill:  c.fill = fill
        c.alignment = Alignment(horizontal=align)
        return c

    run_d = dict(run)
    is_manual = run_d.get("date_from") == "manual"
    date_label = "Manual Run" if is_manual else f"{run_d.get('date_from')} {run_d.get('time_from','00:00')} → {run_d.get('date_to')} {run_d.get('time_to','23:59')}"

    # Title
    s_cell(ws0, 1, 1, "Disco Validator — Run Summary", font=Font(bold=True, size=14, color="FFFFFF"), fill=PatternFill("solid", fgColor="1E3A5F"), align="left")
    ws0.merge_cells("A1:D1")

    # Run info
    row = 3
    s_cell(ws0, row, 1, "Run Period",     font=label_font, fill=grey_fill)
    s_cell(ws0, row, 2, date_label,       fill=grey_fill)
    row += 1
    s_cell(ws0, row, 1, "Generated At",   font=label_font, fill=grey_fill)
    s_cell(ws0, row, 2, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), fill=grey_fill)

    # Overall counts
    row += 2
    s_cell(ws0, row, 1, "Overall", font=section_font)
    row += 1
    for col, h in enumerate(["Metric", "Count"], 1):
        s_cell(ws0, row, col, h, font=Font(bold=True, color="FFFFFF"), fill=PatternFill("solid", fgColor="37474F"), align="center")
    row += 1
    aria_list = [dict(x) for x in aria_results]
    mtx_list  = [dict(x) for x in mtx_results]
    sf_list   = [dict(x) for x in sf_results]
    total = len(aria_list)
    clean = sum(1 for r in aria_list if r.get("validation") == "✅")
    warn  = sum(1 for r in aria_list if r.get("validation") == "⚠️")
    err   = sum(1 for r in aria_list if r.get("validation") not in ("✅", "⚠️") and r.get("validation"))
    for label, val, fill in [("Total Records", total, None), ("Clean ✅", clean, green_fill), ("Action Needed ⚠️", warn, amber_fill), ("Errors ❌", err, red_fill)]:
        s_cell(ws0, row, 1, label, fill=fill)
        s_cell(ws0, row, 2, val,   fill=fill, align="center")
        row += 1

    # Per-system breakdown
    row += 1
    s_cell(ws0, row, 1, "Per-System Breakdown", font=section_font)
    row += 1
    for col, h in enumerate(["System", "Clean ✅", "Action Needed ⚠️", "Errors ❌"], 1):
        s_cell(ws0, row, col, h, font=Font(bold=True, color="FFFFFF"), fill=PatternFill("solid", fgColor="37474F"), align="center")
    row += 1
    systems = [
        ("Aria",        aria_list, "validation"),
        ("Matrixx",     mtx_list,  "mtx_validation"),
        ("SF Asset",    sf_list,   "sf_validation"),
    ]
    for sys_name, sys_list, vkey in systems:
        sc = sum(1 for r in sys_list if r.get(vkey) == "✅")
        sw = sum(1 for r in sys_list if r.get(vkey) == "⚠️")
        se = sum(1 for r in sys_list if r.get(vkey) not in ("✅", "⚠️") and r.get(vkey))
        s_cell(ws0, row, 1, sys_name, font=label_font)
        s_cell(ws0, row, 2, sc, fill=green_fill, align="center")
        s_cell(ws0, row, 3, sw, fill=amber_fill, align="center")
        s_cell(ws0, row, 4, se, fill=red_fill,   align="center")
        row += 1

    # Top flags
    row += 1
    s_cell(ws0, row, 1, "Top Flags", font=section_font)
    row += 1
    for col, h in enumerate(["Flag", "Count"], 1):
        s_cell(ws0, row, col, h, font=Font(bold=True, color="FFFFFF"), fill=PatternFill("solid", fgColor="37474F"), align="center")
    row += 1
    from collections import Counter
    flag_counter = Counter()
    for r in aria_list:
        for f in (r.get("field_to_review") or "").split("|"):
            f = f.strip()
            if f and f != "No_Action_Required":
                flag_counter[f] += 1
    for r in mtx_list:
        for f in (r.get("mtx_field_to_review") or "").split("|"):
            f = f.strip()
            if f and f != "No_Action_Required":
                flag_counter[f] += 1
    for r in sf_list:
        for f in (r.get("sf_field_to_review") or "").split("|"):
            f = f.strip()
            if f and f != "No_Action_Required":
                flag_counter[f] += 1
    if flag_counter:
        for flag, count in flag_counter.most_common(15):
            s_cell(ws0, row, 1, flag)
            s_cell(ws0, row, 2, count, align="center")
            row += 1
    else:
        s_cell(ws0, row, 1, "No flags — all clean ✅", fill=green_fill)

    ws0.column_dimensions["A"].width = 30
    ws0.column_dimensions["B"].width = 45
    ws0.column_dimensions["C"].width = 20
    ws0.column_dimensions["D"].width = 15

    # ── Sheet 1: Aria
    ws1 = wb.create_sheet("Aria Validation")
    aria_headers = [
        "MSISDN", "Order BAN-CAN", "Aria BAN-CAN", "BAN Match",
        "Account Status", "Master Plan Status",
        "Client Plan Instance ID", "Plan Name", "Plan Status", "Plan Status Date",
        "Child Plan 1", "Child Plan 1 ID", "Child Plan 1 Status",
        "Child Start Date", "Child End Date", "Inst String", "Inst Status",
        "No of Inst", "No of Active Inst", "NSO Exist",
        "Status Summary", "Field to Review", "Validation", "Error"
    ]
    style_headers(ws1, aria_headers)
    for row_idx, r in enumerate([dict(x) for x in aria_results], 2):
        aria_ban   = (r.get("ban_can")   or "").strip()
        order_ban  = (r.get("order_ban") or "").strip()
        ban_match  = ("Match" if aria_ban and order_ban and aria_ban == order_ban
                      else "Mismatch" if aria_ban and order_ban else "N/A")
        vals = [
            r.get("msisdn"), r.get("order_ban"), r.get("ban_can"), ban_match,
            r.get("account_status"), r.get("master_plan_status"),
            r.get("client_plan_instance_id"), r.get("plan_name"),
            r.get("plan_status"), r.get("plan_status_date"),
            r.get("child_plan1"), r.get("child_client_plan1_id"),
            r.get("child_plan1_status"), r.get("child_start_date"),
            r.get("child_end_date"), r.get("inst_string"), r.get("inst_status"),
            r.get("no_of_inst"), r.get("no_of_active_inst"), r.get("nso_exist"),
            r.get("status_summary"), r.get("field_to_review"),
            r.get("validation"), r.get("error_msg")
        ]
        for col, val in enumerate(vals, 1):
            ws1.cell(row=row_idx, column=col, value=val)
    color_rows(ws1, [dict(x) for x in aria_results], "validation", aria_headers)
    autofit(ws1)

    # ── Sheet 2: Matrixx
    ws2 = wb.create_sheet("Matrixx Validation")
    mtx_headers = [
        "MSISDN", "BAN-CAN", "Segment", "Payment Pref", "Usage Guide",
        "Subscription", "Subscription ID", "Subscription Status",
        "Plan Name", "Plan Code", "Plan Status", "Plan Charge",
        "Resource ID", "Validation", "Fields to Review", "Error"
    ]
    style_headers(ws2, mtx_headers)
    for row_idx, r in enumerate([dict(x) for x in mtx_results], 2):
        vals = [
            r.get("msisdn"), r.get("mtx_ban_can"), r.get("mtx_segment"),
            r.get("mtx_payment_pref"), r.get("mtx_usage_guide"),
            r.get("mtx_subscription"), r.get("mtx_subscription_id"),
            r.get("mtx_subscription_status"), r.get("mtx_plan_name"),
            r.get("mtx_plan_code"), r.get("mtx_plan_status"),
            r.get("mtx_plan_charge"), r.get("mtx_resource_id"),
            r.get("mtx_validation"), r.get("mtx_field_to_review"), r.get("mtx_error")
        ]
        for col, val in enumerate(vals, 1):
            ws2.cell(row=row_idx, column=col, value=val)
    color_rows(ws2, [dict(x) for x in mtx_results], "mtx_validation", mtx_headers)
    autofit(ws2)

    # ── Sheet 3: SF Asset
    ws3 = wb.create_sheet("SF Asset Validation")
    sf_headers = [
        "MSISDN", "BAN-CAN", "Provisioning Status", "Disconnection Reason",
        "Product", "Last Modified Date", "Asset ID", "Asset Ref ID", "Root Item ID",
        "Validation", "Fields to Review", "Error"
    ]
    style_headers(ws3, sf_headers)
    for row_idx, r in enumerate([dict(x) for x in sf_results], 2):
        vals = [
            r.get("msisdn"), r.get("sf_ban_can"), r.get("sf_provisioning"),
            r.get("sf_disc_reason"), r.get("sf_product"), r.get("sf_last_modified"),
            r.get("sf_asset_id"), r.get("sf_asset_ref_id"), r.get("sf_root_item_id"),
            r.get("sf_validation"), r.get("sf_field_to_review"), r.get("sf_error")
        ]
        for col, val in enumerate(vals, 1):
            ws3.cell(row=row_idx, column=col, value=val)
    color_rows(ws3, [dict(x) for x in sf_results], "sf_validation", sf_headers)
    autofit(ws3)

    # ── Sheet 5: Combined View
    ws5 = wb.create_sheet("Combined View")
    combined_headers = [
        "MSISDN", "Order BANCAN", "Aria BANCAN",
        "Acct Status", "MPI Status", "Plan Name", "Plan Status",
        "Device Status", "Inst Status", "NSO Exist",
        "MTX BANCAN", "MTX Sub Status", "MTX Plan Status", "MTX Device Status",
        "SF BANCAN", "SF Provision", "SF Disc Reason",
        "Overall", "Field to Review"
    ]
    style_headers(ws5, combined_headers)
    aria_map = {r.get("msisdn"): r for r in aria_list}
    mtx_map  = {r.get("msisdn"): r for r in mtx_list}
    sf_map   = {r.get("msisdn"): r for r in sf_list}
    all_msisdns = sorted(set(list(aria_map) + list(mtx_map) + list(sf_map)))
    for row_idx, msisdn in enumerate(all_msisdns, 2):
        a = aria_map.get(msisdn, {})
        m = mtx_map.get(msisdn, {})
        s = sf_map.get(msisdn, {})
        order_bancan = (a.get("order_ban") or "").strip()
        aria_bancan  = (a.get("ban_can")   or "").strip()
        mtx_bancan   = (m.get("mtx_ban_can") or "").strip()
        sf_bancan    = (s.get("sf_ban_can")  or "").strip()
        ref = order_bancan if order_bancan else aria_bancan
        bancan_mismatch = (mtx_bancan and mtx_bancan != ref) or (sf_bancan and sf_bancan != ref)
        if bancan_mismatch:
            ftr = "BANCAN Mismatch"
        else:
            flags = []
            for src in (a.get("field_to_review"), m.get("mtx_field_to_review"), s.get("sf_field_to_review")):
                for f in (src or "").split("|"):
                    f = f.strip()
                    if f and f != "No_Action_Required" and f not in flags:
                        flags.append(f)
            ftr = " | ".join(flags) if flags else "No_Action_Required"
        validations = [a.get("validation") or "", m.get("mtx_validation") or "", s.get("sf_validation") or ""]
        overall = "❌" if any("❌" in v for v in validations) else "⚠️" if any("⚠️" in v for v in validations) else "✅"
        vals = [
            msisdn, order_bancan, aria_bancan,
            a.get("account_status"), a.get("master_plan_status"), a.get("plan_name"), a.get("plan_status"),
            a.get("child_plan1_status"), a.get("inst_status"), a.get("nso_exist"),
            mtx_bancan, m.get("mtx_subscription_status"), m.get("mtx_plan_status"), m.get("mtx_device_status"),
            sf_bancan, s.get("sf_provisioning"), s.get("sf_disc_reason"),
            overall, ftr
        ]
        for col, val in enumerate(vals, 1):
            ws5.cell(row=row_idx, column=col, value=val)
        row_fill = (PatternFill("solid", fgColor="FFEBEE") if overall == "❌"
                    else PatternFill("solid", fgColor="FFF8E1") if overall == "⚠️"
                    else PatternFill("solid", fgColor="E8F5E9"))
        for col in range(1, len(combined_headers) + 1):
            ws5.cell(row=row_idx, column=col).fill = row_fill
    autofit(ws5)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    tf = run_d.get('time_from', '0000').replace(':', '')
    tt = run_d.get('time_to',   '2359').replace(':', '')
    filename = f"DiscoValidator_{run_d['date_from']}_{tf}_to_{run_d['date_to']}_{tt}.xlsx"
    return buf, filename


@app.get("/api/runs/{run_id}/download")
def download_run(run_id: int, request: Request):
    require_session(request)
    conn = get_connection()
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    aria_results = conn.execute("SELECT * FROM results          WHERE run_id = ? ORDER BY msisdn", (run_id,)).fetchall()
    mtx_results  = conn.execute("SELECT * FROM matrixx_results  WHERE run_id = ? ORDER BY msisdn", (run_id,)).fetchall()
    sf_results   = conn.execute("SELECT * FROM sf_asset_results WHERE run_id = ? ORDER BY msisdn", (run_id,)).fetchall()
    conn.close()
    buf, filename = _build_excel_workbook(run, aria_results, mtx_results, sf_results)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.get("/api/matrixx/status")
def matrixx_status():
    import requests as req
    # Check if port-forward process is alive
    pf_alive = _portforward_process is not None and _portforward_process.poll() is None
    try:
        url = f"{matrixx_validator.MATRIXX_BASE_URL.rstrip('/')}/subscription/AccessNumber+test"
        r = req.get(url, timeout=3)
        return {"reachable": True, "pf_alive": pf_alive, "message": "Connected"}
    except Exception:
        if pf_alive:
            return {"reachable": False, "pf_alive": True, "message": "Port-forward running but Matrixx unreachable"}
        else:
            return {"reachable": False, "pf_alive": False, "message": "Port-forward down — re-enter AWS credentials"}


_portforward_process = None
_stop_flags: dict[int, bool] = {}
_inst_stop_flags: dict[int, bool] = {}


@app.post("/api/admin/sf-connect")
def sf_connect(request: Request):
    require_session(request)
    import subprocess, threading
    def run_callback():
        subprocess.Popen(
            ["venv/bin/python", "callback_server.py"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
    threading.Thread(target=run_callback, daemon=True).start()
    login_url = build_login_url()
    return {"login_url": login_url}


@app.post("/api/admin/sf-token")
def sf_update_token(body: SFTokenRequest, request: Request):
    require_session(request)
    from salesforce import _save_token
    _save_token(
        refresh_token=body.refresh_token,
        instance_url=body.instance_url,
        access_token=body.access_token
    )
    try:
        from salesforce import test_connection
        test_connection()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@app.post("/api/admin/matrixx-connect")
def matrixx_connect(body: MatrixxKeysRequest, request: Request):
    require_session(request)
    import subprocess, os as _os
    global _portforward_process
    if _portforward_process and _portforward_process.poll() is None:
        _portforward_process.terminate()
    env = _os.environ.copy()
    env["AWS_ACCESS_KEY_ID"]     = body.aws_access_key_id
    env["AWS_SECRET_ACCESS_KEY"] = body.aws_secret_access_key
    env["AWS_SESSION_TOKEN"]     = body.aws_session_token
    _portforward_process = subprocess.Popen(
        ["kubectl", "port-forward", "svc/rsgateway-ag1", "9090:8080", "-n", "matrixx"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    return {"status": "started", "pid": _portforward_process.pid}


@app.post("/api/admin/matrixx-disconnect")
def matrixx_disconnect(request: Request):
    require_session(request)
    global _portforward_process
    if _portforward_process and _portforward_process.poll() is None:
        _portforward_process.terminate()
        return {"status": "stopped"}
    return {"status": "not_running"}


@app.post("/api/runs/{run_id}/stop")
def stop_run(run_id: int, request: Request):
    require_session(request)
    _stop_flags[run_id] = True
    return {"status": "stop_requested"}


@app.get("/api/runs/{run_id}/matrixx")
def get_matrixx_results(run_id: int, request: Request):
    require_session(request)
    conn = get_connection()
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    results = conn.execute(
        "SELECT * FROM matrixx_results WHERE run_id = ? ORDER BY msisdn", (run_id,)
    ).fetchall()
    conn.close()
    return {"run": dict(run), "results": [dict(r) for r in results]}


@app.get("/api/runs/{run_id}/sf-asset")
def get_sf_asset_results(run_id: int, request: Request):
    require_session(request)
    conn = get_connection()
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    results = conn.execute(
        "SELECT * FROM sf_asset_results WHERE run_id = ? ORDER BY msisdn", (run_id,)
    ).fetchall()
    conn.close()
    return {"run": dict(run), "results": [dict(r) for r in results]}


@app.get("/api/runs/{run_id}/suggestions")
def get_suggestions(run_id: int, request: Request):
    require_session(request)
    conn = get_connection()
    aria_rows = conn.execute(
        "SELECT msisdn, field_to_review FROM results WHERE run_id = ?", (run_id,)
    ).fetchall()
    mtx_rows = conn.execute(
        "SELECT msisdn, mtx_field_to_review FROM matrixx_results WHERE run_id = ?", (run_id,)
    ).fetchall()
    conn.close()

    aria_map = {r["msisdn"]: r["field_to_review"] for r in aria_rows}
    mtx_map  = {r["msisdn"]: r["mtx_field_to_review"] for r in mtx_rows}

    all_msisdns = set(aria_map) | set(mtx_map)
    suggestions = []
    for msisdn in sorted(all_msisdns):
        aria_f = aria_map.get(msisdn, "")
        mtx_f  = mtx_map.get(msisdn, "")
        suggestion = generate_suggestion(aria_f, mtx_f)
        is_clean   = suggestion == "All systems clean — no action required."
        suggestions.append({
            "msisdn":     msisdn,
            "aria":       aria_f or "N/A",
            "matrixx":    mtx_f  or "N/A",
            "suggestion": suggestion,
            "clean":      is_clean,
        })
    return {"suggestions": suggestions}


@app.get("/api/runs/{run_id}/combined")
def get_combined(run_id: int, request: Request):
    require_session(request)
    conn = get_connection()
    rows = conn.execute("""
        SELECT
            r.msisdn,
            r.order_ban,
            r.ban_can          AS aria_bancan,
            r.account_status,
            r.master_plan_status,
            r.plan_name,
            r.plan_status,
            r.child_plan1_status   AS device_status,
            r.inst_status,
            r.nso_exist,
            r.field_to_review,
            r.validation           AS aria_validation,
            m.mtx_ban_can,
            m.mtx_subscription_status,
            m.mtx_plan_status,
            m.mtx_device_status,
            m.mtx_field_to_review,
            m.mtx_validation,
            s.sf_ban_can,
            s.sf_provisioning,
            s.sf_disc_reason,
            s.sf_field_to_review,
            s.sf_validation
        FROM results r
        LEFT JOIN matrixx_results m ON m.run_id = r.run_id AND m.msisdn = r.msisdn
        LEFT JOIN sf_asset_results s ON s.run_id = r.run_id AND s.msisdn = r.msisdn
        WHERE r.run_id = ?
        ORDER BY r.msisdn
    """, (run_id,)).fetchall()
    conn.close()

    combined = []
    for row in rows:
        r = dict(row)
        order_bancan  = r.get("order_ban") or ""
        aria_bancan   = r.get("aria_bancan") or ""
        mtx_bancan    = r.get("mtx_ban_can") or ""
        sf_bancan     = r.get("sf_ban_can") or ""

        # Use order_bancan as reference if available (SF run), else aria_bancan (manual run)
        ref_bancan = order_bancan if order_bancan else aria_bancan

        bancan_mismatch = (
            (mtx_bancan and mtx_bancan != ref_bancan) or
            (sf_bancan  and sf_bancan  != ref_bancan)
        )

        if bancan_mismatch:
            field_to_review = "BANCAN Mismatch"
        else:
            flags = []
            for src in (r.get("field_to_review"), r.get("mtx_field_to_review"), r.get("sf_field_to_review")):
                if src:
                    for f in src.split("|"):
                        f = f.strip()
                        if f and f != "No_Action_Required" and f not in flags:
                            flags.append(f)
            field_to_review = " | ".join(flags) if flags else "No_Action_Required"

        validations = [
            r.get("aria_validation") or "",
            r.get("mtx_validation") or "",
            r.get("sf_validation") or "",
        ]
        if any("❌" in v for v in validations):
            overall = "❌"
        elif any("⚠️" in v for v in validations):
            overall = "⚠️"
        else:
            overall = "✅"

        combined.append({
            "msisdn":              r.get("msisdn"),
            "order_bancan":        order_bancan,
            "aria_bancan":         aria_bancan,
            "account_status":      r.get("account_status"),
            "master_plan_status":  r.get("master_plan_status"),
            "plan_name":           r.get("plan_name"),
            "plan_status":         r.get("plan_status"),
            "device_status":       r.get("device_status"),
            "inst_status":         r.get("inst_status"),
            "nso_exist":           r.get("nso_exist"),
            "mtx_ban_can":         mtx_bancan,
            "mtx_subscription_status": r.get("mtx_subscription_status"),
            "mtx_plan_status":     r.get("mtx_plan_status"),
            "mtx_device_status":   r.get("mtx_device_status"),
            "sf_ban_can":          sf_bancan,
            "sf_provisioning":     r.get("sf_provisioning"),
            "sf_disc_reason":      r.get("sf_disc_reason"),
            "overall":             overall,
            "field_to_review":     field_to_review,
        })

    return {"combined": combined}


def _get_scheduler_config() -> dict:
    conn = get_connection()
    rows = conn.execute("SELECT key, value FROM scheduler_config").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def _apply_scheduler_config(cfg: dict):
    """Reschedule (or remove) both daily report jobs based on current config."""
    # Disconnect Validator job
    enabled  = cfg.get("enabled", "true") == "true"
    hour_utc = (int(cfg.get("trigger_hour", "8")) + 4) % 24
    minute   = int(cfg.get("trigger_minute", "0"))
    if _scheduler.get_job("daily_report"):
        _scheduler.remove_job("daily_report")
    if enabled:
        _scheduler.add_job(
            _scheduled_daily_report,
            CronTrigger(hour=hour_utc, minute=minute, timezone="UTC"),
            id="daily_report", replace_existing=True, misfire_grace_time=3600,
        )

    # Installment Validator job
    inst_enabled  = cfg.get("inst_enabled", "true") == "true"
    inst_hour_utc = (int(cfg.get("inst_trigger_hour", "9")) + 4) % 24
    inst_minute   = int(cfg.get("inst_trigger_minute", "0"))
    if _scheduler.get_job("installment_report"):
        _scheduler.remove_job("installment_report")
    if inst_enabled:
        _scheduler.add_job(
            _scheduled_installment_report,
            CronTrigger(hour=inst_hour_utc, minute=inst_minute, timezone="UTC"),
            id="installment_report", replace_existing=True, misfire_grace_time=3600,
        )


def _check_connectivity() -> list[str]:
    """Return a list of human-readable issues; empty list means all systems go."""
    issues = []
    if not is_authenticated():
        issues.append("Salesforce is not connected — token missing or expired")
    try:
        resp = requests.get(
            f"{matrixx_validator.MATRIXX_BASE_URL.rstrip('/')}/subscription/AccessNumber+test",
            timeout=4,
        )
        _ = resp.status_code
    except Exception:
        issues.append("Matrixx is not reachable — port-forward may be down")
    return issues


def _scheduled_daily_report():
    """Run disconnect validation for the configured date range and email the report."""
    try:
        cfg = _get_scheduler_config()
        if cfg.get("enabled", "true") != "true":
            print("[scheduler] Disabled — skipping.")
            return

        now_ast       = datetime.now(timezone.utc) - timedelta(hours=4)
        yesterday_ast = now_ast - timedelta(days=1)

        report_range = cfg.get("report_range", "yesterday")
        if report_range == "last2days":
            date_from = (now_ast - timedelta(days=2)).strftime("%Y-%m-%d")
        elif report_range == "last7days":
            date_from = (now_ast - timedelta(days=7)).strftime("%Y-%m-%d")
        else:
            date_from = yesterday_ast.strftime("%Y-%m-%d")
        date_to   = yesterday_ast.strftime("%Y-%m-%d")
        date_label = f"{date_from} to {date_to} (AST)"

        # Check connectivity before starting validation
        issues = _check_connectivity()
        if issues:
            print(f"[scheduler] Connectivity issues: {issues} — sending alert email.")
            alert_html = build_connectivity_alert_html(date_label, issues)
            send_alert(
                f"ACTION REQUIRED — {date_label}",
                alert_html,
                smtp_to=cfg.get("smtp_to", ""),
                sender_name=cfg.get("smtp_sender_name", ""),
                subject_prefix=cfg.get("subject_prefix", ""),
            )
            return

        conn = get_connection()
        cur = conn.execute(
            "INSERT INTO runs (triggered_at, date_from, time_from, date_to, time_to, status) VALUES (?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), date_from, "00:00", date_to, "23:59", "running")
        )
        run_id = cur.lastrowid
        conn.commit()
        conn.close()

        do_run(run_id, date_from, "00:00", date_to, "23:59")

        excel_bytes, filename = _build_excel_bytes(run_id)

        conn2 = get_connection()
        aria_list = [dict(r) for r in conn2.execute(
            "SELECT * FROM results WHERE run_id=?", (run_id,)).fetchall()]
        mtx_list  = [dict(r) for r in conn2.execute(
            "SELECT * FROM matrixx_results WHERE run_id=?", (run_id,)).fetchall()]
        sf_list   = [dict(r) for r in conn2.execute(
            "SELECT * FROM sf_asset_results WHERE run_id=?", (run_id,)).fetchall()]
        conn2.close()

        html_body = build_html_body(date_label, aria_list, mtx_list, sf_list)
        send_report(
            date_label, html_body, excel_bytes, filename,
            smtp_to=cfg.get("smtp_to", ""),
            sender_name=cfg.get("smtp_sender_name", ""),
            subject_prefix=cfg.get("subject_prefix", ""),
        )
        print(f"[scheduler] Daily report sent for {date_label}")
    except Exception as exc:
        print(f"[scheduler] Daily report FAILED: {exc}")


def _update_pending(results: list, run_date: str):
    """Insert newly unclean records into installment_pending (skip if OLI already tracked)."""
    conn = get_connection()
    for r in results:
        if r.get("validation") in ("❌", "⚠️") and r.get("oli_id"):
            conn.execute("""
                INSERT OR IGNORE INTO installment_pending
                (oli_id, segment, order_id, sf_bancan, sf_installment_id, msisdn,
                 product_name, order_reason, contract_term,
                 first_seen_date, last_checked_date, days_pending,
                 aria_bancan, aria_oli, aria_installment_id, aria_plan_name,
                 validation, match_status, error, device_amount, monthly_amount)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)
            """, (
                r.get("oli_id"), r.get("segment"), r.get("order_id"),
                r.get("sf_bancan"), r.get("sf_installment_id"), r.get("msisdn"),
                r.get("product_name"), r.get("order_reason"), r.get("contract_term"),
                run_date, run_date,
                r.get("aria_bancan"), r.get("aria_oli"), r.get("aria_installment_id"),
                r.get("aria_plan_name"), r.get("validation"), r.get("match_status"),
                r.get("error"), r.get("device_amount"), r.get("monthly_amount"),
            ))
    conn.commit()
    conn.close()


def _revalidate_all_pending(today: str) -> tuple:
    """Re-validate every pending record against Aria. Returns (still_pending, resolved)."""
    conn = get_connection()
    rows = [dict(r) for r in conn.execute("SELECT * FROM installment_pending").fetchall()]
    conn.close()

    still_pending = []
    resolved     = []

    for r in rows:
        aria = inst_val.check_aria(r["oli_id"])

        if not aria["aria_found"]:
            validation   = "❌"
            match_status = "No Device Found in Aria"
        else:
            bancan_match = (aria["aria_bancan"] or "") == (r.get("sf_bancan") or "")
            inst_match   = (aria["aria_installment_id"] or "") == (r.get("sf_installment_id") or "")
            if bancan_match and inst_match:
                validation   = "✅"
                match_status = "Clean"
            else:
                validation   = "⚠️"
                match_status = "Device Exist with wrong Inst ID/BANCAN"

        if validation == "✅":
            resolved.append({**r, "resolved_date": today})
            conn = get_connection()
            conn.execute("DELETE FROM installment_pending WHERE id=?", (r["id"],))
            conn.commit()
            conn.close()
        else:
            from datetime import date as _date
            try:
                days = (_date.fromisoformat(today) - _date.fromisoformat(r["first_seen_date"])).days + 1
            except Exception:
                days = r.get("days_pending", 1)
            conn = get_connection()
            conn.execute("""
                UPDATE installment_pending
                SET last_checked_date=?, days_pending=?, validation=?, match_status=?,
                    aria_bancan=?, aria_oli=?, aria_installment_id=?, aria_plan_name=?,
                    error=?, device_amount=?, monthly_amount=?
                WHERE id=?
            """, (today, days, validation, match_status,
                  aria["aria_bancan"], aria["aria_oli"], aria["aria_installment_id"],
                  aria["aria_plan_name"], aria["error"],
                  aria.get("device_amount"), aria.get("monthly_amount"), r["id"]))
            conn.commit()
            conn.close()
            still_pending.append({**r, "days_pending": days, "validation": validation,
                                   "match_status": match_status,
                                   "device_amount": aria.get("device_amount"),
                                   "monthly_amount": aria.get("monthly_amount")})

    return still_pending, resolved


def _scheduled_installment_report():
    """Run B2C + B2B installment validation and email the report."""
    try:
        cfg = _get_scheduler_config()
        if cfg.get("inst_enabled", "true") != "true":
            print("[scheduler] Installment report disabled — skipping.")
            return

        now_ast       = datetime.now(timezone.utc) - timedelta(hours=4)
        yesterday_ast = now_ast - timedelta(days=1)
        report_range  = cfg.get("inst_report_range", "yesterday")
        if report_range == "last2days":
            date_from = (now_ast - timedelta(days=2)).strftime("%Y-%m-%d")
        elif report_range == "last7days":
            date_from = (now_ast - timedelta(days=7)).strftime("%Y-%m-%d")
        else:
            date_from = yesterday_ast.strftime("%Y-%m-%d")
        date_to    = yesterday_ast.strftime("%Y-%m-%d")
        date_label = f"{date_from} to {date_to} (AST)"

        issues = _check_connectivity()
        if issues:
            alert_html = build_connectivity_alert_html(date_label, issues)
            send_alert(f"INSTALLMENT — ACTION REQUIRED — {date_label}", alert_html,
                       smtp_to=cfg.get("smtp_to",""), sender_name=cfg.get("smtp_sender_name",""),
                       subject_prefix=cfg.get("inst_subject_prefix",""))
            return

        all_results = []
        for seg in ("B2C", "B2B", "ChangePlan"):
            conn = get_connection()
            cur  = conn.execute(
                "INSERT INTO installment_runs (triggered_at,segment,date_from,date_to,status) VALUES (?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), seg, date_from, date_to, "running"),
            )
            run_id = cur.lastrowid
            conn.commit(); conn.close()
            do_installment_run(run_id, seg, date_from, date_to)
            conn2 = get_connection()
            rows  = conn2.execute("SELECT * FROM installment_results WHERE run_id=?", (run_id,)).fetchall()
            conn2.close()
            all_results.extend([dict(r) for r in rows])

        # Step 1 — add new unclean records to pending
        _update_pending(all_results, date_to)

        # Step 2 — re-validate existing pending records
        still_pending, resolved = _revalidate_all_pending(date_to)

        # Build daily CSV
        import csv as csv_mod
        buf = io.StringIO()
        writer = csv_mod.writer(buf)
        writer.writerow(["Segment","Status","SF BAN-CAN","SF Inst ID","SF OLI ID","SF Order ID",
                         "SF Product Name","Order Reason","Aria BAN-CAN","Aria Inst ID","Aria OLI ID",
                         "Aria Plan","Validation Status","Device Amount","Monthly Amount","Error"])
        for r in all_results:
            writer.writerow([r.get("segment"),r.get("validation"),r.get("sf_bancan"),
                             r.get("sf_installment_id"),r.get("oli_id"),r.get("order_id"),
                             r.get("product_name"),r.get("order_reason"),r.get("aria_bancan"),
                             r.get("aria_installment_id"),r.get("aria_oli"),r.get("aria_plan_name"),
                             r.get("match_status"),r.get("device_amount"),r.get("monthly_amount"),
                             r.get("error")])

        # Append pending section to CSV
        if still_pending:
            writer.writerow([])
            writer.writerow(["--- OUTSTANDING PENDING RECORDS ---"])
            writer.writerow(["Segment","Days Pending","First Seen","SF BAN-CAN","SF Inst ID",
                             "SF OLI ID","SF Order ID","SF Product Name","Order Reason",
                             "Aria BAN-CAN","Aria Inst ID","Aria OLI ID","Aria Plan",
                             "Validation Status","Device Amount","Monthly Amount","Error"])
            for r in still_pending:
                writer.writerow([r.get("segment"),r.get("days_pending"),r.get("first_seen_date"),
                                 r.get("sf_bancan"),r.get("sf_installment_id"),r.get("oli_id"),
                                 r.get("order_id"),r.get("product_name"),r.get("order_reason"),
                                 r.get("aria_bancan"),r.get("aria_installment_id"),r.get("aria_oli"),
                                 r.get("aria_plan_name"),r.get("match_status"),
                                 r.get("device_amount"),r.get("monthly_amount"),r.get("error")])

        csv_bytes = buf.getvalue().encode("utf-8-sig")
        filename  = f"installment_report_{date_from}_{date_to}.csv"

        clean     = sum(1 for r in all_results if r.get("validation")=="✅")
        mismatch  = sum(1 for r in all_results if r.get("validation")=="⚠️")
        not_found = sum(1 for r in all_results if r.get("validation")=="❌")

        # Revenue leak = sum of device_amount across ALL pending records
        revenue_leak = 0.0
        for p in still_pending:
            try:
                revenue_leak += float(str(p.get("device_amount") or "0").replace(",",""))
            except (ValueError, TypeError):
                pass

        html_body = _build_installment_email_html(
            date_label, len(all_results), clean, mismatch, not_found,
            still_pending, resolved, revenue_leak,
        )

        send_report(f"Installment — {date_label}", html_body, csv_bytes, filename,
                    smtp_to=cfg.get("smtp_to",""), sender_name=cfg.get("smtp_sender_name",""),
                    subject_prefix=cfg.get("inst_subject_prefix",""))
        print(f"[scheduler] Installment report sent for {date_label}")
    except Exception as exc:
        print(f"[scheduler] Installment report FAILED: {exc}")


def _build_installment_email_html(date_label, total, clean, mismatch, not_found,
                                   still_pending=None, resolved=None, revenue_leak=0.0):
    still_pending = still_pending or []
    resolved      = resolved or []

    pending_section = ""
    if still_pending:
        rows_html = "".join(
            f"""<tr style="border-bottom:1px solid #21262d">
              <td style="padding:8px 10px;color:#8b949e;font-size:11px">{r.get('segment','')}</td>
              <td style="padding:8px 10px;font-size:11px;color:#f44336;font-weight:700">{r.get('days_pending',1)}d</td>
              <td style="padding:8px 10px;font-size:11px">{r.get('first_seen_date','')}</td>
              <td style="padding:8px 10px;font-size:11px;font-family:monospace">{r.get('oli_id','')}</td>
              <td style="padding:8px 10px;font-size:11px">{r.get('sf_bancan','')}</td>
              <td style="padding:8px 10px;font-size:11px;color:{'#ffaa00' if r.get('validation')=='⚠️' else '#f44336'};font-weight:600">{r.get('match_status','')}</td>
            </tr>"""
            for r in still_pending
        )
        pending_section = f"""
        <tr><td style="padding:0 24px 24px">
          <div style="font-size:14px;font-weight:700;color:#f44336;margin-bottom:12px;padding-top:4px">
            ⚠️ Outstanding Pending Records ({len(still_pending)})
          </div>
          <div style="font-size:12px;color:#8b949e;margin-bottom:10px">
            These records were unclean on a previous run and have not yet been resolved in Aria.
          </div>
          <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;border:1px solid #30363d;border-radius:6px;overflow:hidden">
            <thead>
              <tr style="background:#21262d">
                <th style="padding:8px 10px;font-size:10px;color:#8b949e;text-align:left;text-transform:uppercase">Seg</th>
                <th style="padding:8px 10px;font-size:10px;color:#8b949e;text-align:left;text-transform:uppercase">Age</th>
                <th style="padding:8px 10px;font-size:10px;color:#8b949e;text-align:left;text-transform:uppercase">First Seen</th>
                <th style="padding:8px 10px;font-size:10px;color:#8b949e;text-align:left;text-transform:uppercase">OLI ID</th>
                <th style="padding:8px 10px;font-size:10px;color:#8b949e;text-align:left;text-transform:uppercase">SF BAN-CAN</th>
                <th style="padding:8px 10px;font-size:10px;color:#8b949e;text-align:left;text-transform:uppercase">Status</th>
              </tr>
            </thead>
            <tbody>{rows_html}</tbody>
          </table>
        </td></tr>"""

    resolved_note = ""
    if resolved:
        resolved_note = f"""
        <tr><td style="padding:0 24px 16px">
          <div style="background:rgba(76,175,80,0.08);border:1px solid rgba(76,175,80,0.3);border-radius:6px;padding:10px 14px;font-size:12px;color:#4caf50">
            ✅ {len(resolved)} previously pending record{'s' if len(resolved)!=1 else ''} resolved today
          </div>
        </td></tr>"""

    revenue_leak_section = ""
    if still_pending and revenue_leak > 0:
        count_label = f"{len(still_pending)} outstanding pending record{'s' if len(still_pending) != 1 else ''}"
        revenue_leak_section = f"""
        <tr><td style="padding:0 24px 16px">
          <div style="background:rgba(244,67,54,0.08);border:1px solid rgba(244,67,54,0.4);border-radius:8px;padding:16px 20px">
            <div style="font-size:11px;font-weight:700;color:#f44336;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px">💰 Potential Revenue Leak</div>
            <div style="font-size:26px;font-weight:800;color:#f44336;line-height:1">SAR {revenue_leak:,.2f}</div>
            <div style="font-size:11px;color:#8b949e;margin-top:4px">Sum of device amounts across all {count_label}</div>
          </div>
        </td></tr>"""

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:Arial,sans-serif;color:#e6edf3">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0d1117;padding:32px 16px">
    <tr><td align="center">
      <table width="620" cellpadding="0" cellspacing="0" style="background:#161b22;border-radius:12px;border:1px solid #30363d;overflow:hidden">
        <tr><td style="background:linear-gradient(135deg,#1e3a5f,#0d2137);padding:28px">
          <div style="font-size:20px;font-weight:800;color:#fff">📦 Installment Purchase Validator — Daily Report</div>
          <div style="font-size:13px;color:#90caf9;margin-top:6px">{date_label}</div>
        </td></tr>
        <tr><td style="padding:24px 24px 16px">
          <div style="font-size:12px;font-weight:600;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px">Today's New Orders</div>
          <table width="100%" cellpadding="0" cellspacing="4">
            <tr>
              <td style="text-align:center;background:#1c2128;border-radius:8px;padding:18px;border:1px solid #30363d">
                <div style="font-size:28px;font-weight:800;color:#58a6ff">{total}</div>
                <div style="font-size:10px;color:#8b949e;margin-top:4px;text-transform:uppercase">Total</div>
              </td>
              <td width="8"></td>
              <td style="text-align:center;background:#1c2128;border-radius:8px;padding:18px;border:1px solid #30363d">
                <div style="font-size:28px;font-weight:800;color:#4caf50">{clean}</div>
                <div style="font-size:10px;color:#8b949e;margin-top:4px;text-transform:uppercase">Clean ✅</div>
              </td>
              <td width="8"></td>
              <td style="text-align:center;background:#1c2128;border-radius:8px;padding:18px;border:1px solid #30363d">
                <div style="font-size:28px;font-weight:800;color:#ffaa00">{mismatch}</div>
                <div style="font-size:10px;color:#8b949e;margin-top:4px;text-transform:uppercase">Mismatch ⚠️</div>
              </td>
              <td width="8"></td>
              <td style="text-align:center;background:#1c2128;border-radius:8px;padding:18px;border:1px solid #30363d">
                <div style="font-size:28px;font-weight:800;color:#f44336">{not_found}</div>
                <div style="font-size:10px;color:#8b949e;margin-top:4px;text-transform:uppercase">Not Found ❌</div>
              </td>
            </tr>
          </table>
        </td></tr>
        {resolved_note}
        {revenue_leak_section}
        {pending_section}
        <tr><td style="padding:0 24px 20px">
          <div style="border-top:1px solid #30363d;padding-top:14px;font-size:12px;color:#8b949e">
            📎 Full CSV report attached (includes pending records) · Generated by ITOps Automation
          </div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


# Start scheduler and apply persisted config
_scheduler.start()
_apply_scheduler_config(_get_scheduler_config())


@app.get("/api/admin/scheduler-config")
def api_get_scheduler_config(request: Request):
    require_session(request)
    cfg = _get_scheduler_config()
    job      = _scheduler.get_job("daily_report")
    inst_job = _scheduler.get_job("installment_report")
    next_run      = job.next_run_time.isoformat()      if job      and job.next_run_time      else None
    inst_next_run = inst_job.next_run_time.isoformat() if inst_job and inst_job.next_run_time else None
    return {
        **cfg,
        "next_run_utc":      next_run,
        "inst_next_run_utc": inst_next_run,
        "smtp_host":         os.getenv("SMTP_HOST", ""),
        "smtp_user":         os.getenv("SMTP_USER", ""),
    }


@app.post("/api/admin/scheduler-config")
def api_update_scheduler_config(body: SchedulerConfigRequest, request: Request):
    require_session(request)
    updates = {
        "enabled":              "true" if body.enabled else "false",
        "trigger_hour":         str(max(0, min(23, body.trigger_hour))),
        "trigger_minute":       str(max(0, min(59, body.trigger_minute))),
        "report_range":         body.report_range,
        "subject_prefix":       body.subject_prefix,
        "inst_enabled":         "true" if body.inst_enabled else "false",
        "inst_trigger_hour":    str(max(0, min(23, body.inst_trigger_hour))),
        "inst_trigger_minute":  str(max(0, min(59, body.inst_trigger_minute))),
        "inst_report_range":    body.inst_report_range,
        "inst_subject_prefix":  body.inst_subject_prefix,
        "smtp_to":              body.smtp_to,
        "smtp_sender_name":     body.smtp_sender_name,
    }
    conn = get_connection()
    for k, v in updates.items():
        conn.execute("INSERT OR REPLACE INTO scheduler_config (key, value) VALUES (?,?)", (k, v))
    conn.commit()
    conn.close()
    _apply_scheduler_config(updates)
    return {"status": "ok"}


@app.post("/api/admin/report/run-now")
def api_report_run_now(request: Request):
    require_session(request)
    threading.Thread(target=_scheduled_daily_report, daemon=True).start()
    return {"status": "triggered"}


@app.post("/api/admin/installment-report/run-now")
def api_installment_report_run_now(request: Request):
    require_session(request)
    threading.Thread(target=_scheduled_installment_report, daemon=True).start()
    return {"status": "triggered"}


# ── Installment Validator ──────────────────────────────────────────────────────

def do_installment_run(run_id: int, segment: str, date_from: str, date_to: str, time_from: str = "00:00", time_to: str = "23:59"):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    conn = get_connection()
    try:
        if segment == "ChangePlan":
            pairs = inst_val.parse_changeplan_records(
                run_soql_query(inst_val.build_changeplan_soql(date_from, date_to, time_from, time_to))
            )
            tasks = [(inst_val.process_changeplan_order, p) for p in pairs]
        elif segment == "B2C":
            records = inst_val.parse_sf_records(run_soql_query(inst_val.build_b2c_soql(date_from, date_to, time_from, time_to)), "B2C")
            tasks   = [(inst_val.process_single, r) for r in records]
        else:
            records = inst_val.parse_sf_records(run_soql_query(inst_val.build_b2b_soql(date_from, date_to, time_from, time_to)), "B2B")
            tasks   = [(inst_val.process_single, r) for r in records]

        total = len(tasks)
        conn.execute("UPDATE installment_runs SET total=? WHERE id=?", (total, run_id))
        conn.commit()

        done = clean = mismatch = not_found = 0

        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = {ex.submit(fn, item): (fn, item) for fn, item in tasks}
            for future in as_completed(futures):
                if _inst_stop_flags.get(run_id):
                    ex.shutdown(wait=False, cancel_futures=True)
                    break
                res   = future.result()
                done += 1
                if res is None:
                    conn.execute("UPDATE installment_runs SET done=? WHERE id=?", (done, run_id))
                    conn.commit()
                    continue
                v = res.get("validation", "")
                if v == "✅":    clean     += 1
                elif v == "⚠️":  mismatch  += 1
                else:             not_found += 1

                conn.execute("""
                    INSERT INTO installment_results
                    (run_id,segment,order_id,oli_id,sf_bancan,msisdn,product_name,order_reason,
                     contract_term,sf_installment_id,aria_bancan,aria_oli,aria_installment_id,
                     aria_plan_name,validation,match_status,error,device_amount,monthly_amount)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    run_id, res.get("segment"), res.get("order_id"), res.get("oli_id"),
                    res.get("sf_bancan"), res.get("msisdn"), res.get("product_name"),
                    res.get("order_reason"), res.get("contract_term"), res.get("sf_installment_id"),
                    res.get("aria_bancan"), res.get("aria_oli"), res.get("aria_installment_id"),
                    res.get("aria_plan_name"), res.get("validation"), res.get("match_status"),
                    res.get("error"), res.get("device_amount"), res.get("monthly_amount"),
                ))
                conn.execute(
                    "UPDATE installment_runs SET done=?,clean=?,mismatch=?,not_found=? WHERE id=?",
                    (done, clean, mismatch, not_found, run_id),
                )
                conn.commit()

        final = "stopped" if _inst_stop_flags.get(run_id) else "completed"
        conn.execute(
            "UPDATE installment_runs SET status=?,done=?,clean=?,mismatch=?,not_found=? WHERE id=?",
            (final, done, clean, mismatch, not_found, run_id),
        )
        conn.commit()
        trim_old_installment_runs(conn)
    except Exception as exc:
        conn.execute("UPDATE installment_runs SET status='failed' WHERE id=?", (run_id,))
        conn.commit()
        print(f"[installment] run {run_id} FAILED: {exc}")
    finally:
        _inst_stop_flags.pop(run_id, None)
        conn.close()


@app.get("/api/installment-validator/runs")
def get_installment_runs(request: Request):
    require_session(request)
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM installment_runs ORDER BY id DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return {"runs": [dict(r) for r in rows]}


@app.post("/api/installment-validator/run")
def start_installment_run(req: InstallmentRunRequest, background_tasks: BackgroundTasks, request: Request):
    require_session(request)
    if req.segment not in ("B2C", "B2B", "ChangePlan"):
        raise HTTPException(status_code=400, detail="segment must be B2C, B2B, or ChangePlan")
    conn = get_connection()
    cur  = conn.execute(
        "INSERT INTO installment_runs (triggered_at, segment, date_from, date_to, status) VALUES (?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), req.segment, req.date_from, req.date_to, "running"),
    )
    run_id = cur.lastrowid
    conn.commit()
    conn.close()
    background_tasks.add_task(do_installment_run, run_id, req.segment, req.date_from, req.date_to, req.time_from, req.time_to)
    return {"run_id": run_id, "status": "running"}


@app.get("/api/installment-validator/run/{run_id}/status")
def get_installment_run_status(run_id: int, request: Request):
    require_session(request)
    conn = get_connection()
    row  = conn.execute("SELECT * FROM installment_runs WHERE id=?", (run_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")
    return dict(row)


@app.get("/api/installment-validator/run/{run_id}/results")
def get_installment_run_results(run_id: int, request: Request):
    require_session(request)
    conn  = get_connection()
    rows  = conn.execute(
        "SELECT * FROM installment_results WHERE run_id=? ORDER BY id", (run_id,)
    ).fetchall()
    conn.close()
    return {"results": [dict(r) for r in rows]}


@app.get("/api/installment-validator/run/{run_id}/download/csv")
def download_installment_csv(run_id: int, request: Request):
    import csv as csv_mod
    require_session(request)
    conn = get_connection()
    run  = conn.execute("SELECT * FROM installment_runs WHERE id=?", (run_id,)).fetchone()
    rows = conn.execute(
        "SELECT * FROM installment_results WHERE run_id=? ORDER BY id", (run_id,)
    ).fetchall()
    conn.close()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    buf = io.StringIO()
    writer = csv_mod.writer(buf)
    writer.writerow(["Status","SF BAN-CAN","SF Inst ID","SF OLI ID","SF Order ID",
                     "SF Product Name","Order Reason","Aria BAN-CAN","Aria Inst ID","Aria OLI ID",
                     "Aria Plan","Validation Status","Device Amount","Monthly Amount","Error"])
    for r in rows:
        r = dict(r)
        writer.writerow([r.get("validation"), r.get("sf_bancan"), r.get("sf_installment_id"),
                         r.get("oli_id"), r.get("order_id"), r.get("product_name"),
                         r.get("order_reason"), r.get("aria_bancan"), r.get("aria_installment_id"),
                         r.get("aria_oli"), r.get("aria_plan_name"), r.get("match_status"),
                         r.get("device_amount"), r.get("monthly_amount"), r.get("error")])

    run_dict = dict(run)
    filename = f"installment_{run_dict.get('segment','ALL')}_{run_dict.get('date_from','')}.csv"
    content  = buf.getvalue().encode("utf-8-sig")
    from fastapi.responses import Response as FResponse
    return FResponse(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/installment-validator/run/{run_id}/stop")
def stop_installment_run(run_id: int, request: Request):
    require_session(request)
    _inst_stop_flags[run_id] = True
    conn = get_connection()
    conn.execute("UPDATE installment_runs SET status='stopping' WHERE id=? AND status='running'", (run_id,))
    conn.commit()
    conn.close()
    return {"status": "stopping"}


@app.delete("/api/installment-validator/run/{run_id}")
def delete_installment_run(run_id: int, request: Request):
    require_session(request)
    conn = get_connection()
    row = conn.execute("SELECT status FROM installment_runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Run not found")
    if row["status"] == "running":
        conn.close()
        raise HTTPException(status_code=400, detail="Cannot delete a running run — stop it first")
    conn.execute("DELETE FROM installment_results WHERE run_id=?", (run_id,))
    conn.execute("DELETE FROM installment_runs WHERE id=?", (run_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}


# ── Installment Pending Records ────────────────────────────────────────────────

@app.get("/api/installment-validator/pending")
def get_installment_pending(request: Request):
    require_session(request)
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM installment_pending ORDER BY days_pending DESC, first_seen_date ASC"
    ).fetchall()
    conn.close()
    return {"pending": [dict(r) for r in rows]}


@app.delete("/api/installment-validator/pending/{pending_id}")
def delete_installment_pending(pending_id: int, request: Request):
    """Manually mark a pending record as resolved."""
    require_session(request)
    conn = get_connection()
    conn.execute("DELETE FROM installment_pending WHERE id=?", (pending_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}


@app.post("/api/installment-validator/pending/revalidate")
def revalidate_pending(request: Request):
    """Manually trigger re-validation of all pending records."""
    require_session(request)
    from datetime import date as _date
    today = _date.today().isoformat()
    threading.Thread(target=_revalidate_all_pending, args=(today,), daemon=True).start()
    return {"status": "triggered"}


app.mount("/", StaticFiles(directory="static", html=True), name="static")