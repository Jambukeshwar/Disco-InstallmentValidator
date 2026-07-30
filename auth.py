"""
Authentication module for Disco Validator.
- bcrypt password hashing
- Session token management (stored in DB)
- Auto-expiry after 1 hour of inactivity
"""

import os
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

import bcrypt

from database import get_connection

SESSION_TIMEOUT_MINUTES = 60  # auto logout after 1 hour inactivity


# ── User management ────────────────────────────────────────────────────────────

def init_auth_db():
    """Create users and sessions tables if they don't exist."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            last_active TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def create_user(username: str, password: str):
    """Hash password and store user. Raises ValueError if username exists."""
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, hashed, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
    except Exception:
        raise ValueError(f"Username '{username}' already exists.")
    finally:
        conn.close()


def delete_user(username: str):
    """Remove a user and their sessions."""
    conn = get_connection()
    conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
    conn.execute("DELETE FROM users WHERE username = ?", (username,))
    conn.commit()
    conn.close()


def list_users() -> list[str]:
    conn = get_connection()
    rows = conn.execute("SELECT username, created_at FROM users ORDER BY created_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def change_password(username: str, new_password: str):
    hashed = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    conn = get_connection()
    conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (hashed, username))
    conn.commit()
    conn.close()


# ── Login / logout ─────────────────────────────────────────────────────────────

def verify_login(username: str, password: str) -> Optional[str]:
    """
    Verify credentials. Returns a session token on success, None on failure.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT password_hash FROM users WHERE username = ?", (username,)
    ).fetchone()
    conn.close()

    if not row:
        return None
    if not bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
        return None

    # Create session
    token = secrets.token_urlsafe(32)
    conn = get_connection()
    conn.execute(
        "INSERT INTO sessions (token, username, last_active) VALUES (?, ?, ?)",
        (token, username, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()
    return token


def logout(token: str):
    """Delete session."""
    conn = get_connection()
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()


# ── Session validation ─────────────────────────────────────────────────────────

def get_session(token: str) -> Optional[dict]:
    """
    Return session dict if valid and not expired.
    Updates last_active on every call (activity reset).
    Returns None if invalid or expired.
    """
    if not token:
        return None

    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM sessions WHERE token = ?", (token,)
    ).fetchone()

    if not row:
        conn.close()
        return None

    last_active = datetime.fromisoformat(row["last_active"])
    now = datetime.now(timezone.utc)

    if now - last_active > timedelta(minutes=SESSION_TIMEOUT_MINUTES):
        # Expired — delete session
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        conn.close()
        return None

    # Refresh last_active
    conn.execute(
        "UPDATE sessions SET last_active = ? WHERE token = ?",
        (now.isoformat(), token)
    )
    conn.commit()
    conn.close()
    return dict(row)
