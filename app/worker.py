import json
import threading
from datetime import datetime, timedelta, timezone
from .db import now
from .spend import collect, export, oldest_day
from .integrations import RemoteError


def enqueue(db, kind, full=False, force=False):
    with db.connect() as c:
        c.execute("INSERT OR IGNORE INTO jobs(kind,full_scan,force_export,created_at) VALUES(?,?,?,?)", (kind, int(full), int(force), now()))
        c.execute("UPDATE jobs SET full_scan=MAX(full_scan,?),force_export=MAX(force_export,?) WHERE kind=? AND status='queued'", (int(full), int(force), kind))
        return c.execute("SELECT id FROM jobs WHERE kind=? AND status IN ('queued','running')", (kind,)).fetchone()[0]


def recover(db):
    with db.connect() as c:
        c.execute("UPDATE jobs SET status='queued',summary='Відновлено після перезапуску' WHERE status='running'")
        c.execute("UPDATE keitaro_cost_exports SET status='failed',error='Перервано; повторимо повну суму' WHERE status='sending'")


def prune(db):
    # Use the latest account-local date to apply the configured age limit; retain no spend older than it.
    from zoneinfo import ZoneInfo
    settings = db.settings()
    accounts = db.rows("SELECT id,timezone FROM meta_ad_accounts")
    with db.connect() as c:
        for account in accounts:
            cutoff = str(oldest_day(datetime.now(ZoneInfo(account["timezone"])).date(), settings))
            for table in ("spend_latest", "spend_snapshots"):
                c.execute(f"DELETE FROM {table} WHERE ad_account_id=? AND day<?", (account["id"], cutoff))
            c.execute("DELETE FROM keitaro_cost_exports WHERE meta_campaign_id IN (SELECT id FROM meta_campaigns WHERE ad_account_id=?) AND day<?", (account["id"], cutoff))
        cutoff_time = (datetime.now(timezone.utc) - timedelta(days=62)).isoformat()
        c.execute("DELETE FROM logs WHERE at<?", (cutoff_time,))
        c.execute("DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 20000)")
        c.execute("DELETE FROM jobs WHERE finished_at<? AND status NOT IN ('running','queued')", (cutoff_time,))
        c.execute("DELETE FROM sessions WHERE expires_at<?", (now(),))


class Worker:
    def __init__(self, db, config):
        self.db, self.config = db, config
        self.stop_event = threading.Event()
        self.thread = None
        self.last_heartbeat = now()

    def start(self):
        recover(self.db)
        self.thread = threading.Thread(target=self.loop, daemon=True, name="spend-worker")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def tick(self):
        self.last_heartbeat = now()
        s = self.db.settings()
        current = datetime.now(timezone.utc)
        if not s["next_run_at"] or datetime.fromisoformat(s["next_run_at"]) <= current:
            # Persist timer and both queue entries in a single transaction.
            with self.db.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                s = json.loads(c.execute("SELECT value FROM spend_settings WHERE id=1").fetchone()[0])
                for kind, enabled in (("spend-collect", s["facebook_enabled"]), ("spend-export-keitaro", s["keitaro_enabled"])):
                    if enabled:
                        c.execute("INSERT OR IGNORE INTO jobs(kind,created_at) VALUES(?,?)", (kind, now()))
                s["next_run_at"] = (current + timedelta(hours=s["interval_hours"])).isoformat(timespec="seconds")
                c.execute("UPDATE spend_settings SET value=? WHERE id=1", (json.dumps(s),))
            prune(self.db)
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            # Collection first, regardless of request order. One worker serializes both job types.
            job = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY CASE kind WHEN 'spend-collect' THEN 0 ELSE 1 END,id LIMIT 1").fetchone()
            if not job:
                return
            job = dict(job)
            c.execute("UPDATE jobs SET status='running',started_at=? WHERE id=?", (now(), job["id"]))
        self.db.log("info", f"Запуск {job['kind']} #{job['id']}", job["id"])
        try:
            fn = collect if job["kind"] == "spend-collect" else export
            status, summary = fn(self.db, self.config, job)
        except Exception as e:
            status = "error"
            summary = str(e) if isinstance(e, RemoteError) else f"Внутрішня помилка {type(e).__name__}; задача збережена у журналі"
        self.db.execute("UPDATE jobs SET status=?,finished_at=?,summary=? WHERE id=?", (status, now(), summary, job["id"]))
        self.db.log("info" if status == "success" else status, summary, job["id"])

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                # Database unavailable/disk full must not kill the scheduling thread.
                import logging
                logging.getLogger("myraha").error("Worker: storage or scheduling error; retry in 5 seconds")
            self.stop_event.wait(5)
