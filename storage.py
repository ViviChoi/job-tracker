from __future__ import annotations

import sqlite3
import json
import os
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Optional

# Google Sheets imports are optional
try:
    import gspread
    from google.oauth2.service_account import Credentials
    SHEETS_AVAILABLE = True
except ImportError:
    SHEETS_AVAILABLE = False


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.json"
    return json.loads(config_path.read_text())


BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"


def get_db_path() -> Path:
    config = load_config()
    raw_path = config["storage"]["local_path"]
    p = Path(raw_path)
    # 相对路径 → 相对于程序目录，绝对路径/~路径 → 直接用
    path = (BASE_DIR / p) if not p.is_absolute() and not raw_path.startswith("~") else p.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def init_db() -> None:
    db_path = get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                title TEXT,
                company TEXT,
                location TEXT,
                link TEXT,
                posted_at TEXT,
                match_score INTEGER,
                match_reason TEXT,
                source TEXT,
                notified INTEGER DEFAULT 0,
                notified_at TEXT,
                starred INTEGER DEFAULT 0,
                status TEXT DEFAULT 'new',
                notes TEXT DEFAULT '',
                deleted INTEGER DEFAULT 0,
                deleted_at TEXT,
                resume_generated_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migrate existing tables that lack new columns
        existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        migrations = {
            "starred": "ALTER TABLE jobs ADD COLUMN starred INTEGER DEFAULT 0",
            "status": "ALTER TABLE jobs ADD COLUMN status TEXT DEFAULT 'new'",
            "notes": "ALTER TABLE jobs ADD COLUMN notes TEXT DEFAULT ''",
            "deleted": "ALTER TABLE jobs ADD COLUMN deleted INTEGER DEFAULT 0",
            "deleted_at": "ALTER TABLE jobs ADD COLUMN deleted_at TEXT",
            "resume_generated_at": "ALTER TABLE jobs ADD COLUMN resume_generated_at TEXT",
        }
        for col, sql in migrations.items():
            if col not in existing:
                conn.execute(sql)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_notifications (
                id TEXT PRIMARY KEY,
                job_id TEXT,
                scheduled_for TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                reason_text TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def job_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def is_seen(url: str) -> bool:
    db_path = get_db_path()
    jid = job_id(url)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT id FROM jobs WHERE id = ?", (jid,)).fetchone()
    return row is not None


def save_job(job: dict, score: int, reason: str, notified: int = 0) -> None:
    db_path = get_db_path()
    jid = job_id(job["link"])
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            INSERT OR IGNORE INTO jobs
            (id, title, company, location, link, posted_at, match_score, match_reason, source, notified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            jid,
            job.get("title", ""),
            job.get("company", ""),
            job.get("location", ""),
            job.get("link", ""),
            job.get("posted_at", ""),
            score,
            reason,
            job.get("source", "linkedin"),
            notified,
        ))
        conn.commit()

    config = load_config()
    if config["storage"]["google_sheets"]["enabled"]:
        _sync_to_sheets(job, score, reason, jid)


def mark_notified(job_id_str: str) -> None:
    db_path = get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET notified = 1, notified_at = ? WHERE id = ?",
            (datetime.now().isoformat(), job_id_str)
        )
        conn.commit()


def get_pending_jobs() -> list[dict]:
    db_path = get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM jobs WHERE notified = 0 ORDER BY created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_jobs(
    sort_by: str = "created_at",
    sort_dir: str = "desc",
    filter_company: str = "",
    filter_status: str = "",
    filter_starred: bool = False,
    filter_date_from: str = "",
    filter_date_to: str = "",
    trash: bool = False,
) -> list[dict]:
    db_path = get_db_path()
    conditions = ["deleted = ?"]
    params: list = [1 if trash else 0]

    if filter_company:
        conditions.append("LOWER(company) LIKE ?")
        params.append(f"%{filter_company.lower()}%")
    if filter_status:
        conditions.append("status = ?")
        params.append(filter_status)
    if filter_starred:
        conditions.append("starred = 1")
    if filter_date_from:
        conditions.append("created_at >= ?")
        params.append(filter_date_from)
    if filter_date_to:
        conditions.append("created_at <= ?")
        params.append(filter_date_to + " 23:59:59")

    allowed_sorts = {"created_at", "match_score", "company", "title", "posted_at"}
    col = sort_by if sort_by in allowed_sorts else "created_at"
    direction = "DESC" if sort_dir.lower() == "desc" else "ASC"

    where = " AND ".join(conditions)
    sql = f"SELECT * FROM jobs WHERE {where} ORDER BY {col} {direction}"

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def update_job(job_id_str: str, updates: dict) -> None:
    allowed = {"starred", "status", "notes", "resume_generated_at"}
    fields = {k: v for k, v in updates.items() if k in allowed}
    # Auto-stamp resume_generated_at when status transitions to resume_generated
    if updates.get("status") == "resume_generated" and "resume_generated_at" not in fields:
        fields["resume_generated_at"] = datetime.now().isoformat()
    if not fields:
        return
    db_path = get_db_path()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [job_id_str]
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", values)
        conn.commit()


def soft_delete_job(job_id_str: str) -> None:
    db_path = get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET deleted = 1, deleted_at = ? WHERE id = ?",
            (datetime.now().isoformat(), job_id_str),
        )
        conn.commit()


def restore_job(job_id_str: str) -> None:
    db_path = get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET deleted = 0, deleted_at = NULL WHERE id = ?",
            (job_id_str,),
        )
        conn.commit()


def permanent_delete_job(job_id_str: str) -> None:
    db_path = get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id_str,))
        conn.commit()


def save_feedback(job_id_str: str, reason_code: str, reason_text: str = "") -> None:
    db_path = get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO user_feedback (job_id, reason_code, reason_text) VALUES (?, ?, ?)",
            (job_id_str, reason_code, reason_text),
        )
        conn.commit()


def get_feedback_summary() -> list[dict]:
    """Return aggregated feedback for use in AI matching context."""
    db_path = get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT f.reason_code, f.reason_text, j.title, j.company, j.location
            FROM user_feedback f
            LEFT JOIN jobs j ON j.id = f.job_id
            ORDER BY f.created_at DESC
            LIMIT 50
        """).fetchall()
    return [dict(r) for r in rows]


def export_jobs(fmt: str = "csv", trash: bool = False) -> str:
    """Export jobs as CSV or JSON string."""
    jobs = get_jobs(trash=trash)
    export_fields = ["id", "title", "company", "location", "link", "posted_at",
                     "match_score", "match_reason", "status", "starred", "notes", "created_at"]

    if fmt == "json":
        import json as _json
        return _json.dumps([{k: j.get(k) for k in export_fields} for j in jobs],
                           ensure_ascii=False, indent=2)

    # CSV
    import csv, io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=export_fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(jobs)
    return buf.getvalue()


def _sync_to_sheets(job: dict, score: int, reason: str, jid: str) -> None:
    if not SHEETS_AVAILABLE:
        raise RuntimeError("gspread not installed, run: pip install gspread google-auth")

    config = load_config()
    sheets_config = config["storage"]["google_sheets"]
    creds_file = Path(__file__).parent / sheets_config.get("credentials_file", "credentials.json")

    if not creds_file.exists():
        raise FileNotFoundError(f"Google credentials not found: {creds_file}")

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(str(creds_file), scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheets_config["sheets_id"])

    try:
        ws = sh.worksheet("Jobs")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet("Jobs", rows=1000, cols=10)
        ws.append_row(["ID", "Title", "Company", "Location", "Link", "Score", "Reason", "Source", "Created At"])

    ws.append_row([
        jid,
        job.get("title", ""),
        job.get("company", ""),
        job.get("location", ""),
        job.get("link", ""),
        score,
        reason,
        job.get("source", ""),
        datetime.now().isoformat(),
    ])
