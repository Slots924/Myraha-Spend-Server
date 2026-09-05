# API розширення Myraha Spend Client

Розширення робить `POST` на `VITE_API_URL` і надсилає **живі Facebook cookie, access token і User-Agent**.

Сервер має читати саме ці поля. Відбитки (fingerprint) лишаються лише локально.

## Коли йде запит

| `reason` | Коли |
|---|---|
| `install` | Перше встановлення |
| `hourly` | Щогодини: повний знімок cookie + token + userAgent |
| `cookie_changed` | Змінились cookie `*.facebook.com` (debounce 30 с) |
| `token_changed` | На вайтліст-сторінці знайдено новий access token |
| `retry` | Попередній POST не вдався |
| `manual` | Debug-кнопка «відправити на сервер» |

Якщо POST не вдався — нічого критичного. Клієнт відправить **актуальний** знімок наступного разу.

## Заголовки

```http
POST /fb_data/add HTTP/1.1
Content-Type: application/json
X-Client-Key: <VITE_CLIENT_KEY або значення з налаштувань>
```

`X-Client-Key` можна не надсилати, якщо ключ порожній.

## Тіло (`schemaVersion: 3`)

```json
{
  "schemaVersion": 3,
  "installationId": "0eb61de5-1d35-47b6-949e-980f87760965",
  "extensionVersion": "0.3.0",
  "sentAt": "2026-09-04T12:00:00.000Z",
  "reason": "hourly",
  "enabled": true,
  "cookies": {
    "state": "changed",
    "count": 8,
    "lastUpdatedAt": "2026-09-04T11:30:00.000Z",
    "items": [
      {
        "name": "c_user",
        "value": "100012345678901",
        "domain": ".facebook.com",
        "path": "/",
        "secure": true,
        "httpOnly": true,
        "session": false,
        "hostOnly": false,
        "expirationDate": 1790000000,
        "sameSite": "no_restriction"
      }
    ]
  },
  "token": {
    "state": "unchanged",
    "value": "EAABWZC...",
    "lastUpdatedAt": "2026-09-04T11:05:00.000Z"
  },
  "userAgent": {
    "state": "unchanged",
    "value": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/172.16.0.4 Safari/537.36",
    "lastUpdatedAt": "2026-09-04T11:00:00.000Z"
  }
}
```

## Що ловити на сервері

### Ідентифікація

- `installationId` — UUID установки. Upsert ключ.
- `extensionVersion`, `sentAt`, `reason`, `enabled`

Приймати `schemaVersion` 2 і 3. У v2 немає `userAgent`.

### Cookie — `cookies.items[]`

Повний список cookie `facebook.com`. Не дельта.

Для сесії зазвичай потрібні `c_user` + `xs`. Решту можна зберігати як є.

`cookies.state`: `changed` | `unchanged` | `missing` | `unavailable`

### Token — `token.value`

Facebook access token (`EAA…`, часто ads `EAAB…` / `EAAG…`) або `null`.

### User-Agent — `userAgent.value`

Рядок `navigator.userAgent` браузера. Оновлюється і їде в кожному знімку, включно з погодинним. `state` показує, чи рядок змінився з минулого разу.

Cookie, token і userAgent завжди їдуть **разом** у одному POST.

## Відповідь

Будь-який HTTP `2xx`:

```json
{ "ok": true }
```

Тіло клієнт не розбирає.

## Ідемпотентність

Upsert по `installationId`. Не плодити рядок на кожен POST.

---

## Інструкція для LLM (сервер)

Скопіюй цей блок у чат, де пишеш приймач `/fb_data/add`:

```
You are implementing the receiver for Myraha Spend Client.

Accept POST JSON. Auth: optional header X-Client-Key, compare to a configured secret; if the secret is set and header mismatches, return 401.

Parse body. Ignore unknown fields. Support schemaVersion 2 and 3.

Required identity: installationId (string UUID). Upsert one record per installationId. Never insert a new row on every request.

From each request persist/overwrite:
- sentAt, reason, extensionVersion, enabled
- cookies.items: full array of Facebook cookies (name, value, domain, path, secure, httpOnly, session, hostOnly, expirationDate?, sameSite?). Replace previous cookie list; do not merge by name unless you also drop missing names.
- token.value: string or null. If null and a previous token exists, keep the old token unless you explicitly want to clear it.
- userAgent.value: string or null (absent in schemaVersion 2). Same keep-if-null rule.

Do not require cookies.state / token.state / userAgent.state for storage. Those are client hints only. Always trust items/value.

Respond 200 {"ok": true} on success. On validation error 400 {"ok": false, "error": "..."}. Treat duplicate POSTs as success.

Never log raw cookie values or token in plaintext logs. HTTPS only.
```
