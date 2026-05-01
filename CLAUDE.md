# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Disco Validator** is a FastAPI web application that validates telecommunications disconnection records across three backend systems — **Aria** (billing), **Matrixx** (device/network), and **Salesforce** (CRM/assets). It detects cleanup inconsistencies after disconnect orders and provides corrective actions.

## Running the Application

```bash
# Install dependencies (Python 3.x required; bcrypt must also be installed)
pip install -r requirements.txt
pip install bcrypt

# Manage local users
python create_user.py add <username> <password>
python create_user.py list
python create_user.py remove <username>

# Start the main server (http://localhost:8000)
python main.py

# One-time Salesforce OAuth setup (run in a separate terminal, captures callback)
python callback_server.py
```

There are no automated tests or lint configurations in this project.

## Environment Configuration

The `.env` file must be present with:
- **Salesforce**: `SF_CLIENT_ID`, `SF_CLIENT_SECRET`, `SF_INSTANCE_URL`
- **Aria**: `ARIA_CLIENT_NO`, `ARIA_AUTH_KEY`, `ARIA_URL` (AriaQuery endpoint for `get_plan_instance_information_m`), `ARIA_URL_V1` (core/v1 endpoint for all other Aria calls)
- **Matrixx**: `MATRIXX_BASE_URL` (default: `http://localhost:9090/rsgateway/data/json`)
- **Database**: `DB_PATH` (SQLite file path)

Salesforce OAuth tokens are persisted to `auth_data.json` at runtime.

## Architecture

### Two-Tab UI Structure

The frontend (`static/index.html`) is a single-page app with two main tabs:

- **Disconnect Validator** — validates MSISDNs across Aria, Matrixx, and Salesforce Asset; sub-tabs: SF Run (date range from SF SOQL) and Manual Run (paste MSISDNs)
- **Order Validator** — checks NSO installment balance orders per BAN-CAN; sub-tabs: SF Run and Manual Run

### Disconnect Validator Data Flow

```
RunRequest (date range or MSISDN list)
  → Salesforce SOQL query (fetch recent disconnects via salesforce.py)
  → Extract MSISDNs + order BAN-CAN + disconnection reason
  → Concurrent validation via ThreadPoolExecutor (max 3 workers)
      ├── aria_validator.process_single()      → results table
      ├── matrixx_validator.process_single()   → matrixx_results table
      └── sf_asset_validator.process_single()  → sf_asset_results table
  → Poll /api/runs/{run_id}/status until complete
  → Export to Excel via /api/runs/{run_id}/download
```

Disconnect runs retain the **5 most recent**; older runs and their results are auto-deleted.

### Order Validator (NSO) Data Flow

```
NSOValidatorRequest (date range or BAN-CAN list)
  → Optional: SF SOQL to extract BAN-CANs from disconnect orders
  → Sequential per BAN-CAN (_check_nso_for_bancan):
      ├── get_acct_details_all_m  → account_status, master_plan_status,
      │                             last_bill_date, status_date
      ├── get_order_m             → all orders for BAN-CAN
      └── get_order_items_m       → filter items where client_sku contains "Installment_Balance"
  → Store per BAN-CAN in nso_results table
  → Export to Excel via /api/nso-validator/run/{run_id}/download
```

NSO runs retain the **10 most recent**.

### Key Modules

| File | Responsibility |
|------|---------------|
| `main.py` | FastAPI app, all API endpoints, run orchestration, Excel export, NSO validator logic, AI suggestions |
| `aria_validator.py` | Validates billing/plan/installment state in Aria |
| `matrixx_validator.py` | Validates subscription and device state in Matrixx |
| `sf_asset_validator.py` | Validates asset provisioning status in Salesforce |
| `salesforce.py` | Salesforce OAuth2 (PKCE flow) client + SOQL query helper |
| `aria_actions.py` | Corrective actions: cancel plan, cancel installment, create NSO order, activate plan |
| `matrixx_actions.py` | Corrective action: deactivate device |
| `auth.py` | Local user auth: bcrypt password hashing, session tokens (60-min inactivity expiry) |
| `database.py` | SQLite schema init, migrations, and connection management |
| `callback_server.py` | Standalone HTTP server for Salesforce OAuth callback (one-time setup) |

### Aria API Inventory

Two base URLs are used — `ARIA_URL` (AriaQuery) for search/query, `ARIA_URL_V1` (core) for account/order ops:

| API | Used In | Purpose |
|-----|---------|---------|
| `get_plan_instance_information_m` | `aria_validator.py` | Search plan records by MSISDN (`product_field_value`) — drives the entire disconnect validation |
| `get_plan_instance_information_m` | `aria_validator.py` | Fetch child plan details by `client_plan_instance_id` |
| `get_acct_plans_m` | `aria_validator.py` | Get all plans on a BAN-CAN (finds child/device plans) |
| `get_acct_details_all_m` | `aria_validator.py`, `main.py` | Account status (`status_cd`) + master plan status + `last_bill_date` + `status_date` |
| `get_order_m` | `aria_validator.py`, `main.py` | Fetch all orders for a BAN-CAN (NSO existence check) |
| `get_order_items_m` | `aria_validator.py`, `main.py` | Line items per order (filter by `Installment_Balance` SKU) |
| `update_acct_plan_multi_m` | `aria_actions.py` | Cancel main plan (`plan_status_cd=-2`), cancel child plan (`plan_directive=4`), activate plan (`plan_status_cd=1`) |
| `create_order_m` | `aria_actions.py` | Raise NSO installment balance order |

### Validation Logic and `field_to_review` Flags

**Aria** (`aria_validator.py`): Selects the record with the most recent `status_date` when `get_plan_instance_information_m` returns multiple results for the same MSISDN. Flags:
- `Plan_Status` — plan is not Cancelled
- `Device_Status` — child device plan is still active
- `NSO` — unpaid installment exists with no NSO order
- `Installment_Status` — installment balance review needed
- `Possible_reactivation` — plan became active after the disconnect order date
- `No_Action_Required` — all clean

A 3-month exemption applies to old cancelled devices with blank installment strings.

**Matrixx** (`matrixx_validator.py`): Fetches subscription and device concurrently (two threads). Flags:
- `Subscription_Active`, `Plan_Active`, `Device_Active` — active state post-disconnect
- `BANCAN_Mismatch` — Matrixx BAN-CAN doesn't match the original order BAN-CAN
- `No_Action_Required` — all clean

**Salesforce Asset** (`sf_asset_validator.py`): Queries `Asset` object (excludes SIM Card, picks most recent by `LastModifiedDate`). Flags:
- `Provisioning_Not_Disconnected` — `vlocity_cmt__ProvisioningStatus__c` not in `{Deleted, Disconnected}`
- `BANCAN_Mismatch` — SF billing account BAN-CAN doesn't match order
- `Reason_Not_Matched` — `Disconnection_Reason__c` doesn't match order reason
- `Asset_Not_Found` — MSISDN not in SF Assets

**NSO Order Flag** (`main.py` / `renderNSOTable` in `index.html`): `🔴` when order status contains "filled" AND master plan status is "Suspended"; otherwise `✅`.

All validators return one of: `✅` (clean), `⚠️` (action needed), `❌` (error/not found).

### AI Insights (`generate_suggestion` in `main.py`)

Rule-based engine combining `field_to_review` from Aria and Matrixx. Priority order:
1. BAN-CAN mismatch → stop, no actions suggested
2. `Possible_reactivation` → verify before acting
3. If plan is cancelled but NSO/device work remains → Activate plan first, do work, then Cancel plan
4. Normal order: Create NSO → Cancel device → Cancel plan → Matrixx deactivate

### Concurrency and Stop Mechanism

- **Disconnect runs**: `ThreadPoolExecutor` (max 3 workers), global `_stop_flags: dict` keyed by `run_id`
- **NSO runs**: sequential per BAN-CAN in a background thread, global `_nso_stop_flags: dict` keyed by `run_id`
- `POST /api/runs/{run_id}/stop` and `POST /api/nso-validator/run/{run_id}/stop` set the respective stop flags

### Database Schema

- `runs` — Disconnect run metadata (date range, status, progress counts: total/success/error/warn)
- `results` — Aria validation results per MSISDN
- `matrixx_results` — Matrixx validation results per MSISDN
- `sf_asset_results` — Salesforce asset validation results per MSISDN
- `nso_runs` — NSO Validator run metadata (status, total, done)
- `nso_results` — NSO results per BAN-CAN (account_status, master_plan_status, last_bill_date, status_date, nso_orders JSON blob)
- `users` — Local accounts (username, bcrypt hash)
- `sessions` — Active sessions with `last_active` timestamp (60-min inactivity expiry)

`database.py` includes `PRAGMA table_info` migrations that ADD missing columns — safe to run on existing DBs.

### Salesforce OAuth

`salesforce.py` implements PKCE OAuth2. Tokens are auto-refreshed transparently and persisted to `auth_data.json`. PKCE state is persisted to `pkce_state.json`. Initial setup requires running `callback_server.py` to capture the authorization code. The UI also accepts manual token paste via the SF connect modal (`POST /api/admin/sf-token`).

### Matrixx Connectivity

Matrixx is accessed via `kubectl port-forward` on port 9090. `POST /api/admin/matrixx-connect` spawns `matrixx_portforward.sh` using provided AWS credentials; `POST /api/admin/matrixx-disconnect` terminates it. The watchdog script (`matrixx_portforward.sh`) auto-restarts the port-forward if it dies.
