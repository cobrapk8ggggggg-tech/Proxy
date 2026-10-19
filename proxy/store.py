"""Local encrypted credential vault and durable conversation metadata."""

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from cryptography.fernet import Fernet


class Store:
    def __init__(self, directory):
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        keyfile = path / "vault.key"
        secret = os.getenv("VAULT_KEY")
        if not secret:
            try:
                fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(Fernet.generate_key())
            except FileExistsError:
                pass
            secret = keyfile.read_bytes()
        self.cipher = Fernet(secret)
        self.signing_key = hashlib.sha256(
            secret.encode() if isinstance(secret, str) else secret
        ).digest()
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path / "proxy.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, provider TEXT, label TEXT, secret TEXT, fingerprint TEXT UNIQUE, settings TEXT, catalog TEXT, enabled INTEGER, status TEXT, source TEXT, created REAL);
CREATE TABLE IF NOT EXISTS routes(id TEXT PRIMARY KEY, account_id TEXT, config TEXT, enabled INTEGER);
CREATE TABLE IF NOT EXISTS clients(id TEXT PRIMARY KEY, name TEXT, hash TEXT UNIQUE, prefix TEXT, models TEXT, enabled INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, account_id TEXT, client_id TEXT, scope TEXT, explicit_id TEXT, history_hash TEXT, state TEXT, created REAL, updated REAL);
CREATE INDEX IF NOT EXISTS conversation_history ON conversations(client_id,account_id,scope,history_hash);
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, kind TEXT, account_id TEXT, model TEXT, status INTEGER, duration REAL, created REAL);
CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,client_id TEXT,account_id TEXT,name TEXT,attachment TEXT,expires REAL);
CREATE TABLE IF NOT EXISTS responses(id TEXT PRIMARY KEY,client_id TEXT,payload TEXT,expires REAL);
CREATE TABLE IF NOT EXISTS idempotency(id TEXT PRIMARY KEY,body_hash TEXT,payload TEXT,expires REAL);
CREATE TABLE IF NOT EXISTS admin_sessions(hash TEXT PRIMARY KEY, expires REAL);
""")
        self.db.commit()

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def run(self, sql, args=()):
        with self.lock:
            self.db.execute(sql, args)
            self.db.commit()

    def seal(self, value):
        return self.cipher.encrypt(
            json.dumps(value, ensure_ascii=False).encode()
        ).decode()

    def unseal(self, value):
        return json.loads(self.cipher.decrypt(value.encode()))

    def accounts(self):
        return [
            self.public_account(r)
            for r in self.rows("SELECT * FROM accounts ORDER BY created DESC")
        ]

    def public_account(self, row):
        return {
            k: (
                json.loads(v)
                if k in ("catalog", "settings")
                else bool(v) if k == "enabled" else v
            )
            for k, v in row.items()
            if k not in ("secret", "fingerprint")
        }

    def account(self, aid):
        rows = self.rows("SELECT * FROM accounts WHERE id=?", (aid,))
        if not rows:
            raise KeyError(aid)
        row = self.public_account(rows[0])
        row["credential"] = self.unseal(rows[0]["secret"])
        return row

    def add_account(
        self, provider, credential, label="", source="dashboard", enabled=True
    ):
        fingerprint = hashlib.sha256(
            (provider + "\0" + credential).encode()
        ).hexdigest()
        found = self.rows("SELECT id FROM accounts WHERE fingerprint=?", (fingerprint,))
        if found:
            return self.account(found[0]["id"])
        aid = uuid.uuid4().hex
        settings = {
            "project_id": "",
            "conversation_id": "",
            "conversation_mode": "automatic",
            "model": "",
            "reasoning_effort": "auto",
            "thinking_enabled": False,
            "search_enabled": False,
            "gemini_paragraph_mode": False,
            "temperature": 0.7,
            "max_tokens": 16384,
        }
        self.run(
            "INSERT INTO accounts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                aid,
                provider,
                label
                or {
                    "chatgpt": "ChatGPT",
                    "gemini": "Gemini",
                    "deepseek": "DeepSeek Telegram",
                    "qwen": "Qwen",
                }[provider],
                self.seal(credential),
                fingerprint,
                json.dumps(settings),
                "{}",
                int(enabled),
                "pending",
                source,
                time.time(),
            ),
        )
        return self.account(aid)

    def update_account(
        self, aid, settings=None, catalog=None, status=None, label=None, enabled=None
    ):
        for field, value in [
            ("settings", settings),
            ("catalog", catalog),
            ("status", status),
            ("label", label),
            ("enabled", enabled),
        ]:
            if value is not None:
                self.run(
                    f"UPDATE accounts SET {field}=? WHERE id=?",
                    (
                        (
                            json.dumps(value, ensure_ascii=False)
                            if field in ("settings", "catalog")
                            else int(value) if field == "enabled" else value
                        ),
                        aid,
                    ),
                )

    def add_key(self, name, models):
        token = "sk-proxy-" + secrets.token_urlsafe(32)
        kid = uuid.uuid4().hex
        self.run(
            "INSERT INTO clients VALUES(?,?,?,?,?,?,?)",
            (
                kid,
                name,
                hashlib.sha256(token.encode()).hexdigest(),
                token[:15] + "…",
                json.dumps(models),
                1,
                time.time(),
            ),
        )
        return {"id": kid, "key": token, "name": name}

    def client(self, token):
        rows = self.rows(
            "SELECT * FROM clients WHERE hash=? AND enabled=1",
            (hashlib.sha256(token.encode()).hexdigest(),),
        )
        if not rows:
            return None
        row = rows[0]
        row["models"] = json.loads(row["models"])
        return row

    def keys(self):
        return [
            {
                k: json.loads(v) if k == "models" else v
                for k, v in r.items()
                if k != "hash"
            }
            for r in self.rows("SELECT * FROM clients ORDER BY created DESC")
        ]

    def routes(self):
        return [
            {**r, "config": json.loads(r["config"])}
            for r in self.rows("SELECT * FROM routes ORDER BY id")
        ]

    def set_route(self, name, aid, config=None, enabled=True):
        self.run(
            "INSERT INTO routes VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET account_id=excluded.account_id,config=excluded.config,enabled=excluded.enabled",
            (name, aid, json.dumps(config or {}), int(enabled)),
        )

    def event(self, kind, aid="", model="", status=200, duration=0):
        self.run(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, kind, aid, model, status, duration, time.time()),
        )
        self.run(
            "DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY created DESC LIMIT -1 OFFSET 5000)"
        )

    def close(self):
        self.db.close()
