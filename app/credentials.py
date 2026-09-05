import hashlib
import json
from .db import now


def ingest(db, data):
    iid, sent = str(data.installationId), data.sentAt.isoformat()
    cookies = json.dumps([x.model_dump() for x in data.cookies.items], sort_keys=True)
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        old = c.execute("SELECT * FROM installations WHERE id=?", (iid,)).fetchone()
        if old and sent <= old["sent_at"]:
            return
        prior = c.execute("SELECT * FROM api_clients WHERE id=?", (old["client_id"],)).fetchone() if old and old["client_id"] else None
        token = data.token.value or (db.decrypt(prior["token"]) if prior else None)
        ua = data.userAgent.value or (old["user_agent"] if old else "") or ""
        client_id = None
        if token:
            digest = hashlib.sha256(token.encode()).hexdigest()
            client = c.execute("SELECT * FROM api_clients WHERE token_hash=?", (digest,)).fetchone()
            if client:
                client_id = client["id"]
                if sent >= client["credential_at"]:
                    ua = ua or client["user_agent"]
                    changed = db.decrypt(client["cookies"]) != cookies or ua != client["user_agent"]
                    c.execute("UPDATE api_clients SET cookies=?,user_agent=?,credential_at=?,last_seen=?,status=?,error=? WHERE id=?",
                              (db.encrypt(cookies), ua, sent, now(), "unknown" if changed else client["status"],
                               None if changed else client["error"], client_id))
            else:
                client_id = c.execute("INSERT INTO api_clients(token_hash,token,cookies,user_agent,credential_at,last_seen) VALUES(?,?,?,?,?,?)",
                                      (digest, db.encrypt(token), db.encrypt(cookies), ua, sent, now())).lastrowid
        metadata = json.dumps(dict(reason=data.reason, extensionVersion=data.extensionVersion, schemaVersion=data.schemaVersion))
        c.execute("""INSERT INTO installations VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                  client_id=excluded.client_id,sent_at=excluded.sent_at,enabled=excluded.enabled,
                  metadata=excluded.metadata,cookies=excluded.cookies,user_agent=excluded.user_agent""",
                  (iid, client_id, sent, int(data.enabled), metadata, db.encrypt(cookies), ua))


def candidates(db):
    return db.rows("""SELECT a.* FROM api_clients a WHERE a.status!='inactive' AND a.manually_disabled=0
                   AND a.user_agent!='' AND EXISTS(SELECT 1 FROM installations i WHERE i.client_id=a.id AND i.enabled=1)
                   ORDER BY a.id""")
