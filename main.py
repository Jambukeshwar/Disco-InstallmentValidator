import io
import os
from datetime import datetime, timezone

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
from aria_validator import run_validation
from aria_actions import cancel_plan, cancel_installment, create_nso_order, activate_plan
from matrixx_actions import deactivate_device
import matrixx_validator
import sf_asset_validator
from auth import init_auth_db, verify_login, logout as auth_logout, get_session

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


# ── Pydantic models ────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    date_from: str
    time_from: str = "00:00"
    date_to: str
    time_to: str = "23:59"

class ManualRunRequest(BaseModel):
    msisdns: list[str]

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


def _start_callback_server():
    """Start the OAuth callback listener on port 8080 in a background thread."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlparse, parse_qs

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self._respond(404, "<h2>Not found</h2>")
                return
            params = parse_qs(parsed.query)
            code  = params.get("code",  [None])[0]
            state = params.get("state", [None])[0]
            error = params.get("error", [None])[0]
            if error:
                self._respond(400, f"<h2>Login failed</h2><p>{params.get('error_description', [error])[0]}</p>")
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            try:
                handle_callback(code, state)
                self._respond(200, """
                    <h2 style='color:green'>&#x2705; Authenticated!</h2>
                    <p>You can close this tab and return to the app.</p>
                    <script>setTimeout(() => window.close(), 2000)</script>
                """)
            except SalesforceError as e:
                self._respond(400, f"<h2>Error</h2><p>{e}</p>")
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def _respond(self, status, html):
            body = html.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    def _serve():
        try:
            server = HTTPServer(("localhost", 8080), _Handler)
            server.serve_forever()
        except Exception:
            pass

    threading.Thread(target=_serve, daemon=True).start()


@app.get("/api/auth/login")
def auth_login():
    _start_callback_server()
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


# ── Runs ───────────────────────────────────────────────────────────────────────

def trim_old_runs(conn, keep: int = 5):
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

    # ── Sheet 1: Aria
    ws1 = wb.active
    ws1.title = "Aria Validation"
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

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    run_d = dict(run)
    tf = run_d.get('time_from', '0000').replace(':', '')
    tt = run_d.get('time_to',   '2359').replace(':', '')
    filename = f"DiscoValidator_{run_d['date_from']}_{tf}_to_{run_d['date_to']}_{tt}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.get("/api/matrixx/status")
def matrixx_status():
    import requests as req
    try:
        url = f"{matrixx_validator.MATRIXX_BASE_URL.rstrip('/')}/subscription/AccessNumber+test"
        r = req.get(url, timeout=3)
        return {"reachable": True}
    except Exception:
        return {"reachable": False}


_portforward_process = None
_stop_flags: dict[int, bool] = {}


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


app.mount("/", StaticFiles(directory="static", html=True), name="static")