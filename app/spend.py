import calendar
import hashlib
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from .db import now
from .credentials import candidates
from .integrations import Meta, Keitaro, RemoteError, check_proxy


def micros(value):
    n = Decimal(str(value))
    if not n.is_finite() or n < 0 or n > Decimal("1000000000"):
        raise ValueError("Некоректний спенд")
    return int((n * 1000000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def money(value):
    return format(Decimal(value) / 1000000, ".6f")


def with_commission(value, percent):
    return int((Decimal(value) * (1 + Decimal(percent) / 100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def split_even(amount, count, index):
    base, extra = divmod(int(amount), int(count))
    return base + (1 if index < extra else 0)


def oldest_day(today, settings):
    months = today.year * 12 + today.month - 1 - settings["max_age_months"]
    y, m = divmod(months, 12)
    boundary = date(y, m + 1, min(today.day, calendar.monthrange(y, m + 1)[1]))
    return max(date.fromisoformat(settings["earliest_date"]), boundary)


def account_window(account, settings, current, full=False):
    today = current.astimezone(ZoneInfo(account["timezone"])).date()
    lower = oldest_day(today, settings)
    last = account.get("last_collected_day")
    if full or not last:
        return lower, today
    # Catch up missed days even after an outage longer than the regular lookback.
    start = min(today - timedelta(days=settings["lookback_days"]), date.fromisoformat(last))
    return max(start, lower), today


def persist_insights(db, account, rows, start, end, client_id, collected):
    tz = ZoneInfo(account["timezone"])
    timestamp = collected.astimezone(timezone.utc).isoformat(timespec="seconds")
    bucket = timestamp
    records = {}
    for row in rows:
        cid = str(row["campaign_id"])
        day = date.fromisoformat(row["date_start"])
        if not re.fullmatch(r"\d{1,40}", cid) or not start <= day <= end or row["date_start"] != row["date_stop"]:
            raise ValueError("Некоректний денний Insights")
        if row.get("account_currency", account["currency"]) != account["currency"]:
            raise ValueError("Валюта Insights не відповідає акаунту")
        key = (cid, str(day))
        if key in records:
            raise ValueError("Дубль рядка Insights")
        records[key] = (micros(row["spend"]), row.get("campaign_name", cid))
    # A fully fetched, successful response can remove a previously reported spend down to zero.
    for old in db.rows("SELECT meta_campaign_id,day FROM spend_latest WHERE ad_account_id=? AND day BETWEEN ? AND ?", (account["id"], str(start), str(end))):
        records.setdefault((old["meta_campaign_id"], old["day"]), (0, None))
    with db.connect() as c:
        for (cid, day), (amount, name) in records.items():
            local_day = date.fromisoformat(day)
            period_start = datetime.combine(local_day, time.min, tz).astimezone(timezone.utc).isoformat()
            period_end = datetime.combine(local_day + timedelta(days=1), time.min, tz).astimezone(timezone.utc).isoformat()
            prev = c.execute("SELECT spend_micros FROM spend_latest WHERE meta_campaign_id=? AND day=?", (cid, day)).fetchone()
            previous = prev[0] if prev else 0
            if name is not None:
                c.execute("INSERT INTO meta_campaigns VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name", (cid, account["id"], name))
            c.execute("""INSERT INTO spend_snapshots(meta_campaign_id,ad_account_id,day,period_start,period_end,bucket_at,
                      spend_micros,previous_micros,delta_micros,currency,timezone,collected_at,source_account_key)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(meta_campaign_id,day,bucket_at) DO UPDATE SET
                      spend_micros=excluded.spend_micros,delta_micros=excluded.spend_micros-spend_snapshots.previous_micros,
                      collected_at=excluded.collected_at,source_account_key=excluded.source_account_key""",
                      (cid, account["id"], day, period_start, period_end, bucket, amount, previous, amount-previous,
                       account["currency"], account["timezone"], timestamp, client_id))
            c.execute("""INSERT INTO spend_latest VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(meta_campaign_id,day) DO UPDATE SET
                      spend_micros=excluded.spend_micros,currency=excluded.currency,timezone=excluded.timezone,
                      period_start=excluded.period_start,period_end=excluded.period_end,collected_at=excluded.collected_at""",
                      (cid, account["id"], day, period_start, period_end, amount, account["currency"], account["timezone"], timestamp))
        c.execute("UPDATE meta_ad_accounts SET last_collected=?,last_collected_day=? WHERE id=?", (timestamp, str(end), account["id"]))


def collect(db, config, job, meta_factory=Meta, proxy_checker=check_proxy):
    settings = db.settings()
    if not settings["facebook_enabled"]:
        return "success", "Збір Facebook вимкнено"
    proxy = db.one("SELECT * FROM proxies WHERE is_primary=1")
    if not proxy:
        raise RemoteError("Не вибрано основну проксі")
    proxy_checker(db, proxy)
    clients = candidates(db)
    if not clients:
        raise RemoteError("Немає доступних API-клієнтів з User-Agent")
    access, objects, warnings, collected_count = {}, {}, 0, 0
    try:
        for client in clients:
            obj = None
            try:
                obj = meta_factory(db, config, client, proxy)
                accounts = obj.accounts()
                # Validate the entire discovery response before updating access/cache.
                valid = []
                for a in accounts:
                    aid = str(a.get("account_id") or str(a["id"]).removeprefix("act_"))
                    if not re.fullmatch(r"\d{1,40}", aid) or not re.fullmatch(r"[A-Z]{3}", a["currency"]):
                        raise ValueError("Некоректний акаунт")
                    ZoneInfo(a["timezone_name"])
                    valid.append((aid, str(a.get("name", aid)), a["currency"], a["timezone_name"]))
                with db.connect() as c:
                    c.execute("DELETE FROM account_clients WHERE client_id=?", (client["id"],))
                    for a in valid:
                        c.execute("INSERT INTO meta_ad_accounts(id,name,currency,timezone) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,currency=excluded.currency,timezone=excluded.timezone", a)
                        c.execute("INSERT INTO account_clients VALUES(?,?)", (a[0], client["id"]))
                        access.setdefault(a[0], []).append(client["id"])
                db.execute("UPDATE api_clients SET status='active',checked_at=?,error=NULL WHERE id=? AND credential_at=?", (now(), client["id"], client["credential_at"]))
                objects[client["id"]] = obj
            except Exception as e:
                if obj:
                    obj.close()
                warnings += 1
                invalid = isinstance(e, RemoteError) and e.invalid_client
                safe = str(e) if isinstance(e, RemoteError) else "Некоректна відповідь Meta"
                db.execute("UPDATE api_clients SET status=CASE WHEN ? THEN 'inactive' ELSE status END,checked_at=?,error=? WHERE id=? AND credential_at=?",
                           (invalid, now(), safe, client["id"], client["credential_at"]))
                db.log("warning", f"API-клієнт #{client['id']}: {safe}", job["id"])
        invalidated = set()
        for aid, owners in access.items():
            account = db.one("SELECT * FROM meta_ad_accounts WHERE id=?", (aid,))
            start, end = account_window(account, settings, datetime.now(timezone.utc), bool(job["full_scan"]))
            if start > end:
                continue
            success = False
            for client_id in owners:
                if client_id in invalidated:
                    continue
                try:
                    rows = objects[client_id].insights(aid, start, end)
                    persist_insights(db, account, rows, start, end, client_id, datetime.now(timezone.utc))
                    collected_count += 1
                    success = True
                    break
                except Exception as e:
                    warnings += 1
                    safe = str(e) if isinstance(e, RemoteError) else "Не вдалося перевірити або зберегти Insights"
                    if isinstance(e, RemoteError) and e.invalid_client:
                        invalidated.add(client_id)
                        version = next(x["credential_at"] for x in clients if x["id"] == client_id)
                        db.execute("UPDATE api_clients SET status='inactive',error=? WHERE id=? AND credential_at=?", (safe, client_id, version))
                    db.log("warning", f"Акаунт {aid}, клієнт #{client_id}: {safe}; пробуємо наступного", job["id"])
            if not success:
                db.log("error", f"Акаунт {aid}: спенд не зібрано; буде повторено наступного запуску", job["id"])
        status = "warning" if warnings else "success"
        if warnings and not collected_count:
            status = "error"
        return status, f"Зібрано акаунтів: {collected_count}; попереджень: {warnings}"
    finally:
        for obj in objects.values():
            obj.close()


def prepare_exports(db, settings, force=False):
    rows = db.rows("""SELECT l.*,m.keitaro_campaign_id FROM spend_latest l
                      JOIN keitaro_campaign_mapping m ON m.meta_campaign_id=l.meta_campaign_id
                      ORDER BY l.meta_campaign_id,l.day,m.keitaro_campaign_id""")
    groups = {}
    for row in rows:
        groups.setdefault((row["meta_campaign_id"], row["day"]), []).append(row)
    with db.connect() as c:
        for group in groups.values():
            row = group[0]
            today = datetime.now(ZoneInfo(row["timezone"])).date()
            if not oldest_day(today, settings) <= date.fromisoformat(row["day"]) <= today:
                continue
            total = with_commission(row["spend_micros"], settings["commission_percent"])
            n = len(group)
            for index, row in enumerate(group):
                amount = split_even(total, n, index)
                fingerprint = hashlib.sha256(json.dumps([amount, n, row["currency"], row["timezone"], row["period_start"], row["period_end"]]).encode()).hexdigest()
                old = c.execute("SELECT * FROM keitaro_cost_exports WHERE meta_campaign_id=? AND keitaro_campaign_id=? AND day=?", (row["meta_campaign_id"], row["keitaro_campaign_id"], row["day"])).fetchone()
                # Reapply recent days on the hourly schedule: late clicks can change distribution even if spend is unchanged.
                recent = date.fromisoformat(row["day"]) >= today - timedelta(days=settings["lookback_days"])
                due = old and old["sent_at"] and datetime.fromisoformat(old["sent_at"]) <= datetime.now(timezone.utc) - timedelta(hours=1)
                status = "pending" if not old or old["fingerprint"] != fingerprint or (recent and (due or force)) else old["status"]
                c.execute("""INSERT INTO keitaro_cost_exports(meta_campaign_id,keitaro_campaign_id,day,period_start,period_end,
                          spend_micros,currency,timezone,fingerprint,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                          ON CONFLICT(meta_campaign_id,keitaro_campaign_id,day) DO UPDATE SET period_start=excluded.period_start,
                          period_end=excluded.period_end,spend_micros=excluded.spend_micros,currency=excluded.currency,
                          timezone=excluded.timezone,fingerprint=excluded.fingerprint,status=excluded.status,updated_at=excluded.updated_at""",
                          (row["meta_campaign_id"], row["keitaro_campaign_id"], row["day"], row["period_start"], row["period_end"],
                           amount, row["currency"], row["timezone"], fingerprint, status, now()))


def export(db, config, job, keitaro_factory=Keitaro):
    settings = db.settings()
    if not settings["keitaro_enabled"]:
        return "success", "Експорт Keitaro вимкнено"
    if not job.get("force_export") and settings["last_export_at"] and datetime.fromisoformat(settings["last_export_at"]) > datetime.now(timezone.utc) - timedelta(minutes=30):
        return "warning", "Експорт відкладено: між запусками Keitaro потрібно 30 хвилин"
    api = keitaro_factory(config)
    sent, errors = 0, 0
    try:
        # Refresh the whole eligible mapping range. Do not use a partial report on pagination failure.
        today = datetime.now(timezone.utc).date()
        mapping = list(api.mappings(oldest_day(today, settings) - timedelta(days=1), today + timedelta(days=1)))
        with db.connect() as c:
            c.execute("DELETE FROM keitaro_campaign_mapping")
            c.executemany("INSERT OR IGNORE INTO keitaro_campaign_mapping VALUES(?,?,?)", [(m, k, now()) for m, k in mapping])
        prepare_exports(db, settings, force=bool(job.get("force_export")))
        db.settings_update({"last_export_at": now()})
        pending = db.rows("SELECT e.* FROM keitaro_cost_exports e WHERE status IN ('pending','failed') ORDER BY day,id")
        for row in pending:
            local_today = datetime.now(ZoneInfo(row["timezone"])).date()
            if date.fromisoformat(row["day"]) < oldest_day(local_today, settings):
                db.execute("UPDATE keitaro_cost_exports SET status='expired',error='Поза дозволеним періодом' WHERE id=?", (row["id"],))
                continue
            if not db.one("SELECT 1 FROM keitaro_campaign_mapping WHERE meta_campaign_id=? AND keitaro_campaign_id=?", (row["meta_campaign_id"], row["keitaro_campaign_id"])):
                db.execute("UPDATE keitaro_cost_exports SET status='failed',error='Відповідність відсутня у звіті Keitaro' WHERE id=?", (row["id"],))
                errors += 1
                continue
            db.execute("UPDATE keitaro_cost_exports SET status='sending',attempts=attempts+1,error=NULL WHERE id=?", (row["id"],))
            try:
                api.update_costs(row)
                db.execute("UPDATE keitaro_cost_exports SET status='sent',sent_at=?,error=NULL WHERE id=?", (now(), row["id"]))
                sent += 1
            except Exception as e:
                safe = str(e) if isinstance(e, RemoteError) else "Помилка обробки відповіді Keitaro"
                db.execute("UPDATE keitaro_cost_exports SET status='failed',error=? WHERE id=?", (safe, row["id"]))
                db.log("error", f"Експорт #{row['id']}, Meta {row['meta_campaign_id']} → Keitaro {row['keitaro_campaign_id']}: {safe}", job["id"])
                errors += 1
        missing = db.one("SELECT COUNT(DISTINCT l.meta_campaign_id) n FROM spend_latest l LEFT JOIN keitaro_campaign_mapping m ON m.meta_campaign_id=l.meta_campaign_id WHERE m.meta_campaign_id IS NULL")["n"]
        if missing:
            db.log("warning", f"Без відповідності у Keitaro: {missing} кампаній. Перевірте sub_id_2 та наявність кліків", job["id"])
        return ("warning" if errors or missing else "success"), f"Відправлено: {sent}; помилок: {errors}; без відповідності: {missing}"
    finally:
        api.close()
