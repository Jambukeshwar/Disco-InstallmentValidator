import sqlite3
import os

DB_PATH = os.getenv("DB_PATH", "nso_validator.db")

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            triggered_at TEXT NOT NULL,
            date_from TEXT NOT NULL,
            time_from TEXT NOT NULL DEFAULT '00:00',
            date_to TEXT NOT NULL,
            time_to TEXT NOT NULL DEFAULT '23:59',
            status TEXT NOT NULL DEFAULT 'running',
            total_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0,
            warn_count INTEGER DEFAULT 0,
            finished_at TEXT
        )
    """)

    # Migrate existing runs table
    existing_runs = {row[1] for row in c.execute("PRAGMA table_info(runs)")}
    if "time_from" not in existing_runs:
        c.execute("ALTER TABLE runs ADD COLUMN time_from TEXT NOT NULL DEFAULT '00:00'")
    if "time_to" not in existing_runs:
        c.execute("ALTER TABLE runs ADD COLUMN time_to TEXT NOT NULL DEFAULT '23:59'")
    if "warn_count" not in existing_runs:
        c.execute("ALTER TABLE runs ADD COLUMN warn_count INTEGER DEFAULT 0")

    c.execute("""
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            msisdn TEXT,
            order_ban TEXT,
            ban_can TEXT,
            account_status TEXT,
            master_plan_status TEXT,
            client_plan_instance_id TEXT,
            plan_name TEXT,
            plan_status TEXT,
            plan_status_date TEXT,
            child_plan1 TEXT,
            child_client_plan1_id TEXT,
            child_plan1_status TEXT,
            child_start_date TEXT,
            child_end_date TEXT,
            inst_string TEXT,
            inst_status TEXT,
            no_of_inst INTEGER,
            no_of_active_inst INTEGER,
            nso_exist TEXT,
            status_summary TEXT,
            field_to_review TEXT,
            validation TEXT,
            error_msg TEXT,
            FOREIGN KEY (run_id) REFERENCES runs(id)
        )
    """)

    # Migrate existing results table
    existing_results = {row[1] for row in c.execute("PRAGMA table_info(results)")}
    if "order_ban" not in existing_results:
        c.execute("ALTER TABLE results ADD COLUMN order_ban TEXT")

    c.execute("""
        CREATE TABLE IF NOT EXISTS matrixx_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            msisdn TEXT,
            mtx_order_ban TEXT,
            mtx_ban_can TEXT,
            mtx_segment TEXT,
            mtx_payment_pref TEXT,
            mtx_usage_guide TEXT,
            mtx_subscription TEXT,
            mtx_subscription_id TEXT,
            mtx_subscription_status TEXT,
            mtx_plan_name TEXT,
            mtx_plan_code TEXT,
            mtx_plan_status TEXT,
            mtx_plan_charge TEXT,
            mtx_resource_id TEXT,
            mtx_device_status TEXT,
            mtx_validation TEXT,
            mtx_field_to_review TEXT,
            mtx_error TEXT,
            FOREIGN KEY (run_id) REFERENCES runs(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS sf_asset_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            msisdn TEXT,
            sf_asset_id TEXT,
            sf_ban_can TEXT,
            sf_provisioning TEXT,
            sf_disc_reason TEXT,
            sf_product TEXT,
            sf_asset_ref_id TEXT,
            sf_root_item_id TEXT,
            sf_last_modified TEXT,
            sf_validation TEXT,
            sf_field_to_review TEXT,
            sf_error TEXT,
            FOREIGN KEY (run_id) REFERENCES runs(id)
        )
    """)

    # Migrate matrixx_results
    existing_mtx = {row[1] for row in c.execute("PRAGMA table_info(matrixx_results)")}
    if "mtx_device_status" not in existing_mtx:
        c.execute("ALTER TABLE matrixx_results ADD COLUMN mtx_device_status TEXT")
    if "mtx_order_ban" not in existing_mtx:
        c.execute("ALTER TABLE matrixx_results ADD COLUMN mtx_order_ban TEXT")

    # Migrate sf_asset_results
    existing_sf = {row[1] for row in c.execute("PRAGMA table_info(sf_asset_results)")}
    if "sf_last_modified" not in existing_sf:
        c.execute("ALTER TABLE sf_asset_results ADD COLUMN sf_last_modified TEXT")

    conn.commit()
    conn.close()