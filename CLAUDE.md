# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Disco Validator** is a FastAPI web application that validates telecommunications disconnection records across three backend systems — **Aria** (billing), **Matrixx** (device/network), and **Salesforce** (CRM/assets). It detects cleanup inconsistencies and provides corrective actions.

## Running the Application

```bash
# Install dependencies (Python 3.x required)
pip install -r requirements.txt

# Manage local users
python create_user.py add <username> <password>
python create_user.py list
python create_user.py remove <username>

# Start the main server (http://localhost:8000)
python main.py

# One-time Salesforce OAuth setup (run in a separate terminal)
python callback_server.py
```

There are no automated tests or lint configurations in this project.

## Environment Configuration

The `.env` file must be present with:
- **Salesforce**: `SF_CLIENT_ID`, `SF_CLIENT_SECRET`, `SF_INSTANCE_URL`
- **Aria**: `ARIA_CLIENT_NO`, `ARIA_AUTH_KEY`, `ARIA_URL` (billing ops), `ARIA_ADMIN_URL` (admin ops)
- **Matrixx**: `MATRIXX_BASE_URL` (default: `http://localhost:9090/rsgateway/data/json`)
- **Database**: `DB_PATH` (SQLite file path)

Salesforce OAuth tokens are persisted to `auth_data.json` at runtime.

## Architecture

### Data Flow

```
RunRequest (date range or MSISDN list)
  → Salesforce SOQL query (fetch recent disconnects)
  → Extract MSISDNs + order metadata
  → Concurrent validation via ThreadPoolExecutor (max 3 workers)
      ├── aria_validator.process_single()
      ├── matrixx_validator.process_single()
      └── sf_asset_validator.process_single()
  → Store results in SQLite
  → Poll /api/runs/{run_id}/status until complete
  → Export to Excel via /api/runs/{run_id}/download
```

### Key Modules

| File | Responsibility |
|------|---------------|
| `main.py` | FastAPI app, all API endpoints, run orchestration, Excel export |
| `aria_validator.py` | Validates billing/plan/installment state in Aria |
| `matrixx_validator.py` | Validates subscription and device state in Matrixx |
| `sf_asset_validator.py` | Validates asset provisioning status in Salesforce |
| `salesforce.py` | Salesforce OAuth2 (PKCE flow) client + SOQL query helper |
| `aria_actions.py` | Corrective actions: cancel plan, cancel installment, create NSO order, activate plan |
| `matrixx_actions.py` | Corrective action: deactivate device |
| `auth.py` | Local user auth: bcrypt password hashing, session tokens (60-min expiry) |
| `database.py` | SQLite schema init and connection management |
| `callback_server.py` | Standalone HTTP server for Salesforce OAuth callback (one-time setup) |

### Database Schema

- `runs` — Run metadata (date range, status, progress counts)
- `results` — Aria validation results per MSISDN
- `matrixx_results` — Matrixx validation results per MSISDN
- `sf_asset_results` — Salesforce asset validation results per MSISDN
- `users` — Local accounts (username, bcrypt hash)
- `sessions` — Active sessions with expiry timestamps

The system retains only the 5 most recent runs; older runs and their results are auto-deleted.

### Validation Logic

**Aria** (`aria_validator.py`): Checks most recent plan status (should be Cancelled), child device plan status, and installment balance. Flags active plans, unpaid installments without an NSO order, and potential reactivations (plan became active after the disconnect order). A 3-month exemption applies to old cancelled devices with blank installments.

**Matrixx** (`matrixx_validator.py`): Fetches subscription and device concurrently. Flags active subscriptions, active plans, or active devices. Also checks BAN-CAN mismatch against the original order.

**Salesforce Asset** (`sf_asset_validator.py`): Queries the `Asset` object for the MSISDN. Flags non-Deleted/non-Disconnected provisioning status, and BAN-CAN mismatches.

Validators return one of: `✅` (clean), `⚠️` (action needed), `❌` (error/not found).

### Concurrency and Stop Mechanism

Run processing uses `ThreadPoolExecutor` with a global `_stop_flags` dict keyed by run ID. Calling `POST /api/runs/{run_id}/stop` sets the flag, causing pending futures to be cancelled and the run status set to `stopped`.

### Salesforce OAuth

`salesforce.py` implements PKCE OAuth2. Tokens are auto-refreshed transparently and persisted to `auth_data.json`. Initial setup requires running `callback_server.py` to capture the authorization code.

### Matrixx Connectivity

Matrixx is accessed via `kubectl port-forward`. The admin endpoint `POST /api/admin/matrixx-connect` spawns this process using provided AWS credentials; `POST /api/admin/matrixx-disconnect` terminates it.
