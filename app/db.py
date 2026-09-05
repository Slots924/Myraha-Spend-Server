import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from cryptography.fernet import Fernet


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


DEFAULTS = dict(interval_hours=1, facebook_enabled=True, keitaro_enabled=True,
                commission_percent="10", earliest_date="2026-09-01", max_age_months=2,
                lookback_days=5, next_run_at="", last_export_at="")

SCHEMA = """
CREATE TABLE IF NOT EXISTS spend_settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS api_clients (
 id INTEGER PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, token TEXT NOT NULL,
 cookies TEXT NOT NULL, user_agent TEXT NOT NULL, credential_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'unknown', manually_disabled INTEGER NOT NULL DEFAULT 0,
 last_seen TEXT NOT NULL, checked_at TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS installations (
 id TEXT PRIMARY KEY, client_id INTEGER REFERENCES api_clients(id), sent_at TEXT NOT NULL,
 enabled INTEGER NOT NULL, metadata TEXT NOT NULL, cookies TEXT NOT NULL, user_agent TEXT);
CREATE TABLE IF NOT EXISTS proxies (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, protocol TEXT NOT NULL, host TEXT NOT NULL,
 port INTEGER NOT NULL, username TEXT NOT NULL, password TEXT NOT NULL, refresh_url TEXT NOT NULL,
 is_primary INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'unknown',
 ip TEXT, checked_at TEXT, error TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_primary_proxy ON proxies(is_primary) WHERE is_primary=1;
CREATE TABLE IF NOT EXISTS meta_ad_accounts (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, currency TEXT NOT NULL, timezone TEXT NOT NULL,
 last_collected TEXT, last_collected_day TEXT);
CREATE TABLE IF NOT EXISTS account_clients (
 account_id TEXT REFERENCES meta_ad_accounts(id), client_id INTEGER REFERENCES api_clients(id),
 PRIMARY KEY(account_id,client_id));
CREATE TABLE IF NOT EXISTS meta_campaigns (
 id TEXT PRIMARY KEY, ad_account_id TEXT REFERENCES meta_ad_accounts(id), name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS spend_snapshots (
 id INTEGER PRIMARY KEY, meta_campaign_id TEXT NOT NULL, ad_account_id TEXT NOT NULL,
 day TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL, bucket_at TEXT NOT NULL,
 spend_micros INTEGER NOT NULL, previous_micros INTEGER NOT NULL, delta_micros INTEGER NOT NULL,
 currency TEXT NOT NULL, timezone TEXT NOT NULL, collected_at TEXT NOT NULL, source_account_key INTEGER NOT NULL,
 UNIQUE(meta_campaign_id,day,bucket_at));
CREATE TABLE IF NOT EXISTS spend_latest (
 meta_campaign_id TEXT NOT NULL, ad_account_id TEXT NOT NULL, day TEXT NOT NULL,
 period_start TEXT NOT NULL, period_end TEXT NOT NULL, spend_micros INTEGER NOT NULL,
 currency TEXT NOT NULL, timezone TEXT NOT NULL, collected_at TEXT NOT NULL,
 PRIMARY KEY(meta_campaign_id,day));
CREATE TABLE IF NOT EXISTS keitaro_campaign_mapping (
 meta_campaign_id TEXT NOT NULL, keitaro_campaign_id INTEGER NOT NULL, seen_at TEXT NOT NULL,
 PRIMARY KEY(meta_campaign_id,keitaro_campaign_id));
CREATE TABLE IF NOT EXISTS keitaro_cost_exports (
 id INTEGER PRIMARY KEY, meta_campaign_id TEXT NOT NULL, keitaro_campaign_id INTEGER NOT NULL,
 day TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
 spend_micros INTEGER NOT NULL, currency TEXT NOT NULL, timezone TEXT NOT NULL,
 fingerprint TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 sent_at TEXT, error TEXT, updated_at TEXT NOT NULL,
 UNIQUE(meta_campaign_id,keitaro_campaign_id,day));
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
 full_scan INTEGER NOT NULL DEFAULT 0, force_export INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, summary TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_job ON jobs(kind) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS logs (
 id INTEGER PRIMARY KEY, at TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL, job_id INTEGER);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, expires_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS latest_day ON spend_latest(day);
CREATE INDEX IF NOT EXISTS snapshots_day ON spend_snapshots(day);
"""


class DB:
    def __init__(self, path, key):
        self.path = path
        self.cipher = Fernet(key.encode())
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            if 'force_export' not in {r[1] for r in c.execute('PRAGMA table_info(jobs)')}:
                c.execute('ALTER TABLE jobs ADD COLUMN force_export INTEGER NOT NULL DEFAULT 0')
            c.execute("INSERT OR IGNORE INTO spend_settings VALUES(1,?)", (json.dumps(DEFAULTS),))

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=FULL")
        try:
            with c:
                yield c
        finally:
            c.close()

    def rows(self, sql, params=()):
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=()):
        with self.connect() as c:
            return c.execute(sql, params).lastrowid

    def settings(self):
        return {**DEFAULTS, **json.loads(self.one("SELECT value FROM spend_settings WHERE id=1")["value"])}

    def settings_update(self, patch):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            s = {**DEFAULTS, **json.loads(c.execute("SELECT value FROM spend_settings WHERE id=1").fetchone()[0]), **patch}
            c.execute("UPDATE spend_settings SET value=? WHERE id=1", (json.dumps(s),))

    def encrypt(self, value):
        return self.cipher.encrypt(value.encode()).decode()

    def decrypt(self, value):
        return self.cipher.decrypt(value.encode()).decode()

    def log(self, level, message, job_id=None):
        # Callers supply only controlled messages and IDs, never remote bodies/URLs/exceptions.
        self.execute("INSERT INTO logs(at,level,message,job_id) VALUES(?,?,?,?)", (now(), level, message, job_id))
