import json
import re
import time
from urllib.parse import quote, urlsplit, unquote
import requests
from .models import ProxyInput
from .db import now


class RemoteError(Exception):
    """Only safe, constructed messages; do not retain request URLs or remote bodies."""
    def __init__(self, message, invalid_client=False, transport=False):
        super().__init__(message)
        self.invalid_client = invalid_client
        self.transport = transport


def parse_proxy(raw):
    raw = raw.strip()
    match = re.fullmatch(r"(.*?)(?:\[(https://[^\[\]]+)\])?", raw)
    if not match:
        raise ValueError("Некоректний формат проксі")
    address, refresh = match.groups()
    if "://" not in address:
        address = "http://" + address
    scheme, rest = address.split("://", 1)
    scheme = "socks5h" if scheme == "socks5" else scheme
    if "@" in rest:
        u = urlsplit(address)
        host, port, username, password = u.hostname, u.port, unquote(u.username or ""), unquote(u.password or "")
    else:
        parts = rest.split(":", 3)
        if len(parts) not in (2, 4):
            raise ValueError("Формат: host:port:user:password[https://refresh-url]")
        host, port = parts[:2]
        username, password = parts[2:] if len(parts) == 4 else ("", "")
    return ProxyInput(protocol=scheme, host=host, port=port, username=username, password=password, refresh_url=refresh or "").model_dump()


def save_proxy(db, data, proxy_id=None):
    values = (data.name, data.protocol, data.host, data.port, data.username, db.encrypt(data.password), db.encrypt(data.refresh_url))
    if proxy_id:
        old = db.one("SELECT * FROM proxies WHERE id=?", (proxy_id,))
        if not old:
            raise ValueError("Проксі не знайдено")
        # Empty password from edit form means preserve existing secret.
        if not data.password:
            values = values[:5] + (old["password"],) + values[6:]
        db.execute("UPDATE proxies SET name=?,protocol=?,host=?,port=?,username=?,password=?,refresh_url=?,status='unknown' WHERE id=?", values + (proxy_id,))
        return proxy_id
    return db.execute("INSERT INTO proxies(name,protocol,host,port,username,password,refresh_url) VALUES(?,?,?,?,?,?,?)", values)


def session_for_proxy(db, proxy):
    if not proxy:
        raise RemoteError("Не вибрано основну проксі", transport=True)
    password = db.decrypt(proxy["password"])
    auth = f"{quote(proxy['username'], safe='')}:{quote(password, safe='')}@" if proxy["username"] else ""
    address = f"{proxy['protocol']}://{auth}{proxy['host']}:{proxy['port']}"
    session = requests.Session()
    session.trust_env = False
    session.proxies = {"http": address, "https": address}
    return session


def request_json(session, method, url, *, meta=False, **kwargs):
    for attempt in range(3):
        try:
            r = session.request(method, url, timeout=(10, 60), allow_redirects=False, **kwargs)
        except requests.RequestException:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RemoteError("Мережева помилка / таймаут; перевірте проксі або сервер", transport=True) from None
        try:
            body = r.json()
        except ValueError:
            body = None
        if meta and isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = body["error"]
            code = error.get("code")
            code = code if isinstance(code, int) else 0
            sub = error.get("error_subcode")
            sub = sub if isinstance(sub, int) else 0
            transient = error.get("is_transient") or code in (1, 2, 4, 17, 32, 613)
            if transient and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RemoteError(f"Meta error {code}, subcode {sub}", invalid_client=code in (102, 190) and not transient)
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            time.sleep(2 ** attempt)
            continue
        if not 200 <= r.status_code < 300:
            raise RemoteError(f"HTTP {r.status_code}; перевірте налаштування інтеграції", transport=meta)
        if not isinstance(body, dict):
            raise RemoteError("Сервер повернув неочікуваний формат відповіді", transport=meta)
        return body
    raise RemoteError("Вичерпано спроби")


def check_proxy(db, proxy):
    try:
        with session_for_proxy(db, proxy) as session:
            body = request_json(session, "GET", "https://api.ipify.org?format=json")
        import ipaddress
        ip = str(ipaddress.ip_address(body["ip"]))
        db.execute("UPDATE proxies SET status='active',ip=?,checked_at=?,error=NULL WHERE id=?", (ip, now(), proxy["id"]))
        return ip
    except (RemoteError, ValueError, KeyError):
        db.execute("UPDATE proxies SET status='error',checked_at=?,error=? WHERE id=?", (now(), "Перевірка проксі не вдалася", proxy["id"]))
        raise RemoteError("Перевірка проксі не вдалася", transport=True) from None


class Meta:
    def __init__(self, db, config, client, proxy):
        self.session = session_for_proxy(db, proxy)
        self.base = f"https://graph.facebook.com/{config.meta_version}/"
        self.session.headers.update({"Authorization": "Bearer " + db.decrypt(client["token"]), "User-Agent": client["user_agent"]})
        for cookie in json.loads(db.decrypt(client["cookies"])):
            if cookie.get("expirationDate") and cookie["expirationDate"] < time.time():
                continue
            self.session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"], secure=cookie["secure"])

    def close(self):
        self.session.close()

    def get(self, path, params=None):
        return request_json(self.session, "GET", self.base + path, meta=True, params=params or {})

    def pages(self, path, params):
        params = dict(params)
        seen = set()
        for _ in range(10000):
            body = self.get(path, params)
            if not isinstance(body.get("data"), list):
                raise RemoteError("Meta: відсутній масив data")
            yield from body["data"]
            paging = body.get("paging", {})
            if not paging.get("next"):
                return
            # Never follow arbitrary paging URLs carrying credentials; use a cursor on the fixed Graph host.
            after = paging.get("cursors", {}).get("after")
            if not after:
                from urllib.parse import parse_qs
                after = parse_qs(urlsplit(paging["next"]).query).get("after", [None])[0]
            if not after or after in seen:
                raise RemoteError("Meta: некоректна пагінація; дані не збережено")
            seen.add(after)
            params["after"] = after
        raise RemoteError("Meta: перевищено ліміт сторінок")

    def accounts(self):
        self.get("me", {"fields": "id"})
        return list(self.pages("me/adaccounts", {"fields": "id,account_id,name,currency,timezone_name", "limit": 100}))

    def insights(self, account_id, start, end):
        return list(self.pages(f"act_{account_id}/insights", {
            "fields": "campaign_id,campaign_name,spend,date_start,date_stop,account_currency",
            "level": "campaign", "time_increment": 1, "limit": 500,
            "time_range": json.dumps({"since": str(start), "until": str(end)}),
        }))


class Keitaro:
    def __init__(self, config):
        u = urlsplit(config.keitaro_url)
        if u.scheme != "https" or not u.hostname or u.username or u.query or u.fragment or not config.keitaro_key:
            raise RemoteError("Заповніть HTTPS KEITARO_URL та KEITARO_API_KEY у .env")
        self.base = config.keitaro_url.rstrip("/") + "/admin_api/v1"
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers["Api-Key"] = config.keitaro_key

    def close(self):
        self.session.close()

    def mappings(self, start, end):
        offset, seen = 0, set()
        while offset < 1000000:
            body = request_json(self.session, "POST", self.base + "/report/build", json={
                "range": {"from": str(start) + " 00:00:00", "to": str(end) + " 23:59:59", "timezone": "UTC"},
                "dimensions": ["campaign_id", "sub_id_2"], "measures": ["clicks"],
                "sort": [{"name": "campaign_id", "order": "ASC"}, {"name": "sub_id_2", "order": "ASC"}],
                "limit": 1000, "offset": offset,
            })
            rows = body.get("rows")
            if not isinstance(rows, list):
                raise RemoteError("Keitaro report: відсутній масив rows")
            if not rows:
                return
            marker = json.dumps(rows, sort_keys=True)
            if marker in seen:
                raise RemoteError("Keitaro: повтор сторінки звіту")
            seen.add(marker)
            for row in rows:
                meta_id = str(row.get("sub_id_2", "")).strip()
                kid = str(row.get("campaign_id", ""))
                if re.fullmatch(r"\d{1,40}", meta_id) and kid.isdigit() and int(kid) > 0:
                    yield meta_id, int(kid)
            offset += len(rows)
            if len(rows) < 1000:
                return
        raise RemoteError("Keitaro: перевищено ліміт звіту")

    def update_costs(self, row):
        from .spend import money
        body = request_json(self.session, "POST", self.base + f"/campaigns/{row['keitaro_campaign_id']}/update_costs", json={
            "start_date": row["day"] + " 00:00:00", "end_date": row["day"] + " 23:59:59",
            "cost": money(row["spend_micros"]), "currency": row["currency"], "timezone": row["timezone"],
            "filters": {"sub_id_2": row["meta_campaign_id"]},
        })
        if body.get("success") is not True:
            raise RemoteError("Keitaro не підтвердив success=true; запис буде повторено")
