import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal
from pathlib import Path
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request, Depends, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .config import Config, ROOT
from .db import DB, now
from .models import Ingest, Settings, ProxyInput
from .credentials import ingest
from .integrations import parse_proxy, save_proxy, check_proxy, RemoteError
from .spend import money, with_commission
from .worker import Worker, enqueue


class Login(BaseModel):
    password: str = Field(max_length=1024)


class RawProxy(BaseModel):
    value: str = Field(max_length=4096)


class ClientAction(BaseModel):
    action: Literal["enable", "disable", "recheck"]


def create_app(config=None):
    config = config or Config.env()
    db = DB(config.database, config.encryption_key)
    if config.seed_proxy and not db.one("SELECT 1 FROM proxies") and not db.settings().get("proxy_seeded"):
        pid = save_proxy(db, ProxyInput(**parse_proxy(config.seed_proxy)))
        db.execute("UPDATE proxies SET is_primary=1 WHERE id=?", (pid,))
        db.settings_update({"proxy_seeded": True})
    worker = Worker(db, config)

    @asynccontextmanager
    async def lifespan(app):
        if config.scheduler:
            worker.start()
        yield
        worker.stop()

    app = FastAPI(title="Myraha Spend", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db, app.state.worker = db, worker
    attempts = defaultdict(deque)
    rate_lock = threading.Lock()
    proxy_lock = threading.Lock()

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic errors include input values by default; never return credentials.
        fields = [".".join(map(str, e["loc"])) for e in exc.errors()]
        return JSONResponse({"ok": False, "error": "Некоректні поля: " + ", ".join(fields)}, status_code=400)

    @app.exception_handler(RemoteError)
    async def remote_error(request, exc):
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        return JSONResponse({"ok": False, "error": "Внутрішня помилка сервера; перевірте журнал служби"}, status_code=500)

    @app.middleware("http")
    async def security(request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"error": "Потрібен application/json"}, status_code=415)
            length = request.headers.get("content-length")
            if length and (not length.isdigit() or int(length) > 1048576):
                return JSONResponse({"error": "Завеликий запит"}, status_code=413)
            # Stream limit also applies to chunked requests, not just Content-Length.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 1048576:
                    return JSONResponse({"error": "Завеликий запит"}, status_code=413)
            request._body = bytes(body)
            if request.url.path != "/fb_data/add":
                origin = request.headers.get("origin")
                if origin and origin != str(request.base_url).rstrip("/"):
                    return JSONResponse({"error": "Недозволений Origin"}, status_code=403)
                if request.headers.get("x-requested-with") != "Myraha":
                    return JSONResponse({"error": "Потрібен X-Requested-With"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    def auth(request: Request):
        token = request.cookies.get("myraha_session", "")
        digest = hashlib.sha256(token.encode()).hexdigest()
        if not token or not db.one("SELECT 1 FROM sessions WHERE token_hash=? AND expires_at>?", (digest, now())):
            raise HTTPException(401, "Введіть пароль")

    @app.get("/healthz")
    def health():
        db.one("SELECT 1")
        if config.scheduler and (not worker.thread or not worker.thread.is_alive()):
            raise HTTPException(503, "Worker unavailable")
        return {"ok": True}

    @app.post("/api/login")
    def login(data: Login, request: Request):
        key, current = request.client.host, time.monotonic()
        with rate_lock:
            for ip in list(attempts):
                if not attempts[ip] or current - attempts[ip][-1] > 900:
                    del attempts[ip]
            if len(attempts) > 10000:
                raise HTTPException(429, "Спробуйте пізніше")
            q = attempts[key]
            while q and current-q[0] > 900:
                q.popleft()
            if len(q) >= 10:
                raise HTTPException(429, "Забагато спроб. Зачекайте 15 хвилин")
            q.append(current)
        if not hmac.compare_digest(data.password.encode(), config.password.encode()):
            raise HTTPException(401, "Невірний пароль")
        with rate_lock:
            attempts.pop(key, None)
        token = secrets.token_urlsafe(48)
        db.execute("INSERT INTO sessions VALUES(?,?)", (hashlib.sha256(token.encode()).hexdigest(), (datetime.now(timezone.utc)+timedelta(hours=12)).isoformat()))
        response = JSONResponse({"ok": True})
        response.set_cookie("myraha_session", token, max_age=43200, httponly=True, secure=config.secure_cookie, samesite="strict")
        return response

    @app.post("/api/logout", dependencies=[Depends(auth)])
    def logout(request: Request):
        db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(request.cookies["myraha_session"].encode()).hexdigest(),))
        response = JSONResponse({"ok": True})
        response.delete_cookie("myraha_session")
        return response

    @app.post("/fb_data/add")
    def receive(data: Ingest, request: Request):
        if config.client_key and not hmac.compare_digest(request.headers.get("x-client-key", "").encode(), config.client_key.encode()):
            raise HTTPException(401, "Невірний X-Client-Key")
        ingest(db, data)
        return {"ok": True}

    @app.get("/api/settings", dependencies=[Depends(auth)])
    def get_settings():
        return {**db.settings(), "keitaro_configured": bool(config.keitaro_url and config.keitaro_key), "meta_version": config.meta_version}

    @app.put("/api/settings", dependencies=[Depends(auth)])
    def put_settings(data: Settings):
        patch = data.model_dump(mode="json")
        old = db.settings()
        if old["interval_hours"] != data.interval_hours:
            patch["next_run_at"] = (datetime.now(timezone.utc)+timedelta(hours=data.interval_hours)).isoformat()
        db.settings_update(patch)
        db.log("info", "Налаштування оновлено")
        return {"ok": True}

    @app.get("/api/clients", dependencies=[Depends(auth)])
    def clients(status: Literal["all", "active", "inactive", "unknown"] = "all", page: int = Query(1, ge=1), limit: int = Query(25, ge=1, le=100)):
        where, args = ("", []) if status == "all" else ("WHERE a.status=?", [status])
        total = db.one(f"SELECT COUNT(*) n FROM api_clients a {where}", args)["n"]
        rows = db.rows(f"SELECT a.*, (SELECT COUNT(*) FROM installations i WHERE i.client_id=a.id AND i.enabled=1) enabled_installations FROM api_clients a {where} ORDER BY a.id DESC LIMIT ? OFFSET ?", args+[limit,(page-1)*limit])
        for row in rows:
            token = db.decrypt(row.pop("token"))
            row["token_preview"] = token[:6] + "…" + token[-3:] if len(token) > 12 else "••••"
            cookies = json.loads(db.decrypt(row.pop("cookies")))
            row["cookie_preview"] = "; ".join(f"{x['name']}={x['value'][:2]}…" for x in cookies[:8])
            row["cookie_count"] = len(cookies)
            row.pop("token_hash")
        return {"items": rows, "total": total, "page": page, "limit": limit}

    @app.post("/api/clients/{client_id}", dependencies=[Depends(auth)])
    def client_action(client_id: int, data: ClientAction):
        if not db.one("SELECT id FROM api_clients WHERE id=?", (client_id,)):
            raise HTTPException(404, "Клієнт не знайдений")
        if data.action == "disable":
            db.execute("UPDATE api_clients SET manually_disabled=1 WHERE id=?", (client_id,))
        else:
            db.execute("UPDATE api_clients SET manually_disabled=0,status='unknown',error=NULL WHERE id=?", (client_id,))
        return {"ok": True}

    @app.get("/api/proxies", dependencies=[Depends(auth)])
    def proxies():
        rows = db.rows("SELECT * FROM proxies ORDER BY is_primary DESC,id")
        for row in rows:
            row["has_password"] = bool(db.decrypt(row.pop("password")))
            row["refresh_url"] = db.decrypt(row["refresh_url"])
        return rows

    @app.post("/api/proxies/parse", dependencies=[Depends(auth)])
    def parse(data: RawProxy):
        try:
            return parse_proxy(data.value)
        except (ValueError, TypeError):
            raise HTTPException(400, "Формат: http://host:port:user:password[https://refresh-url]") from None

    @app.post("/api/proxies", dependencies=[Depends(auth)])
    def add_proxy(data: ProxyInput):
        return {"ok": True, "id": save_proxy(db, data)}

    @app.put("/api/proxies/{pid}", dependencies=[Depends(auth)])
    def edit_proxy(pid: int, data: ProxyInput):
        try:
            save_proxy(db, data, pid)
        except ValueError:
            raise HTTPException(404, "Проксі не знайдено") from None
        return {"ok": True}

    @app.delete("/api/proxies/{pid}", dependencies=[Depends(auth)])
    def delete_proxy(pid: int):
        db.execute("DELETE FROM proxies WHERE id=?", (pid,))
        return {"ok": True}

    @app.post("/api/proxies/{pid}/{action}", dependencies=[Depends(auth)])
    def proxy_action(pid: int, action: Literal["primary", "check", "refresh"]):
        proxy = db.one("SELECT * FROM proxies WHERE id=?", (pid,))
        if not proxy:
            raise HTTPException(404, "Проксі не знайдено")
        if action == "primary":
            with db.connect() as c:
                c.execute("UPDATE proxies SET is_primary=0")
                c.execute("UPDATE proxies SET is_primary=1 WHERE id=?", (pid,))
            return {"ok": True}
        if not proxy_lock.acquire(blocking=False):
            raise HTTPException(409, "Перевірка проксі вже виконується")
        try:
            if action == "check":
                return {"ok": True, "ip": check_proxy(db, proxy)}
            if db.one("SELECT id FROM jobs WHERE kind='spend-collect' AND status='running'"):
                raise HTTPException(409, "Зачекайте завершення збору перед зміною IP")
            url = db.decrypt(proxy["refresh_url"])
            if not url:
                raise HTTPException(400, "Не задано URL зміни IP")
            # Provider control endpoint is not Facebook; it may need a direct connection.
            import requests
            try:
                with requests.Session() as session:
                    session.trust_env = False
                    r = session.get(url, timeout=(10,30), allow_redirects=False)
                    if not 200 <= r.status_code < 300:
                        raise RemoteError(f"Провайдер refresh повернув HTTP {r.status_code}")
            except requests.RequestException:
                raise RemoteError("Не вдалося звернутися до провайдера зміни IP") from None
            db.execute("UPDATE proxies SET status='unknown',error=NULL WHERE id=?", (pid,))
            return {"ok": True, "message": "Запит зміни IP прийнято. За кілька секунд натисніть Перевірити"}
        finally:
            proxy_lock.release()

    @app.post("/api/run/{kind}", dependencies=[Depends(auth)])
    def run(kind: Literal["all", "collect", "export", "full"]):
        settings = db.settings()
        live = db.rows("SELECT id FROM jobs WHERE status IN ('queued','running')")
        if live and kind in ("all", "full"):
            return {"ok": True, "job_ids": [row["id"] for row in live], "already_running": True}
        ids = []
        if kind in ("all", "collect", "full") and settings["facebook_enabled"]:
            ids.append(enqueue(db, "spend-collect", full=kind == "full", force=True))
        if kind in ("all", "export", "full") and settings["keitaro_enabled"]:
            ids.append(enqueue(db, "spend-export-keitaro", force=True))
        return {"ok": True, "job_ids": ids}

    @app.get("/api/jobs", dependencies=[Depends(auth)])
    def jobs():
        return db.rows("SELECT * FROM jobs ORDER BY id DESC LIMIT 50")

    @app.get("/api/logs", dependencies=[Depends(auth)])
    def logs(level: Literal["all", "info", "warning", "error"] = "all", page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=200)):
        where, args = ("", []) if level == "all" else ("WHERE level=?", [level])
        return {"items": db.rows(f"SELECT * FROM logs {where} ORDER BY id DESC LIMIT ? OFFSET ?", args+[limit,(page-1)*limit]),
                "total": db.one(f"SELECT COUNT(*) n FROM logs {where}", args)["n"]}

    @app.get("/api/exports", dependencies=[Depends(auth)])
    def exports(status: Literal["all", "pending", "failed", "sent", "sending", "expired"] = "all", page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=100)):
        where, args = ("", []) if status == "all" else ("WHERE status=?", [status])
        rows = db.rows(f"SELECT * FROM keitaro_cost_exports {where} ORDER BY id DESC LIMIT ? OFFSET ?", args+[limit,(page-1)*limit])
        for row in rows:
            row["spend"] = money(row.pop("spend_micros"))
        return {"items": rows, "total": db.one(f"SELECT COUNT(*) n FROM keitaro_cost_exports {where}", args)["n"]}

    @app.get("/api/dashboard", dependencies=[Depends(auth)])
    def dashboard():
        settings = db.settings()
        totals, chart = {}, {}
        for row in db.rows("SELECT * FROM spend_latest"):
            today = datetime.now(ZoneInfo(row["timezone"])).date().isoformat()
            t = totals.setdefault(row["currency"], dict(today=0, month=0, today_commission=0, month_commission=0))
            if row["day"] == today:
                t["today"] += row["spend_micros"]
                t["today_commission"] += with_commission(row["spend_micros"], settings["commission_percent"])
            if row["day"][:7] == today[:7]:
                t["month"] += row["spend_micros"]
                t["month_commission"] += with_commission(row["spend_micros"], settings["commission_percent"])
            key = (row["day"], row["currency"])
            chart[key] = chart.get(key, 0) + row["spend_micros"]
        exports = db.rows("SELECT status,COUNT(*) count FROM keitaro_cost_exports GROUP BY status")
        missing = db.rows("SELECT l.meta_campaign_id,c.name,l.currency,SUM(l.spend_micros) amount FROM spend_latest l LEFT JOIN keitaro_campaign_mapping m ON m.meta_campaign_id=l.meta_campaign_id LEFT JOIN meta_campaigns c ON c.id=l.meta_campaign_id WHERE m.meta_campaign_id IS NULL GROUP BY l.meta_campaign_id,l.currency ORDER BY amount DESC LIMIT 100")
        for row in missing:
            row["amount"] = money(row["amount"])
        return {
            "totals": {currency:{key:money(v) for key,v in total.items()} for currency,total in totals.items()},
            "chart": [{"day":day,"currency":currency,"spend":money(v)} for (day,currency),v in sorted(chart.items())],
            "clients": db.rows("SELECT status,COUNT(*) count FROM api_clients GROUP BY status"),
            "accounts": db.one("SELECT COUNT(*) n FROM meta_ad_accounts")["n"],
            "campaigns": db.one("SELECT COUNT(*) n FROM meta_campaigns")["n"],
            "exports": exports, "missing": missing,
            "last_collected": db.one("SELECT MAX(collected_at) at FROM spend_latest")["at"],
            "next_run_at": settings["next_run_at"], "commission_percent": settings["commission_percent"],
            "proxy": db.one("SELECT name,status,ip FROM proxies WHERE is_primary=1"),
        }

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static/index.html")

    return app
