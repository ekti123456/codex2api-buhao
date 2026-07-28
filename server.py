from __future__ import annotations

import hashlib
import hmac
import http.cookies
import json
import math
import os
import secrets
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    base_url: str
    admin_key: str
    admin_password: str
    database_file: Path
    secure_cookie: bool
    session_seconds: int
    http_timeout: int
    account_verify_seconds: int

    @classmethod
    def from_env(cls) -> "Config":
        required = {
            "CODEX2API_BASE_URL": os.getenv("CODEX2API_BASE_URL", "").strip().rstrip("/"),
            "CODEX2API_ADMIN_KEY": os.getenv("CODEX2API_ADMIN_KEY", "").strip(),
            "POOL_MANAGER_ADMIN_PASSWORD": os.getenv("POOL_MANAGER_ADMIN_PASSWORD", ""),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError("缺少环境变量: " + ", ".join(missing))
        upstream_url = urllib.parse.urlsplit(required["CODEX2API_BASE_URL"])
        if upstream_url.scheme not in {"http", "https"} or not upstream_url.netloc:
            raise RuntimeError("CODEX2API_BASE_URL 必须是完整的 http/https 地址")
        return cls(
            host=os.getenv("POOL_MANAGER_HOST", "127.0.0.1"),
            port=configured_port(),
            base_url=required["CODEX2API_BASE_URL"],
            admin_key=required["CODEX2API_ADMIN_KEY"],
            admin_password=required["POOL_MANAGER_ADMIN_PASSWORD"],
            database_file=Path(os.getenv("POOL_MANAGER_DATABASE_FILE", str(ROOT / "data" / "pool-manager.sqlite3"))),
            secure_cookie=os.getenv("POOL_MANAGER_SECURE_COOKIE", "false").lower() == "true",
            session_seconds=int(os.getenv("POOL_MANAGER_SESSION_HOURS", "12")) * 3600,
            http_timeout=int(os.getenv("POOL_MANAGER_HTTP_TIMEOUT_SECONDS", "30")),
            account_verify_seconds=max(5, min(300, int(os.getenv("POOL_MANAGER_ACCOUNT_VERIFY_SECONDS", "60")))),
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def configured_port() -> int:
    # PaaS 平台通常注入 PORT 或 WEB_PORT；Docker Compose 则使用 POOL_MANAGER_PORT。
    for name in ("PORT", "WEB_PORT", "POOL_MANAGER_PORT"):
        value = os.getenv(name, "").strip()
        if value.isdigit() and 1 <= int(value) <= 65535:
            return int(value)
    return 8790


def default_settings() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": iso_now(),
        "global": {
            "evaluation_interval_minutes": 5,
            "account_health_interval_minutes": 10,
            "replenish_cooldown_minutes": 60,
            "max_accounts_per_run": 20,
            "trigger_mode": "any",
            "supplier_auto_import": False,
        },
        "groups": [],
    }


class Manager:
    def __init__(self, config: Config):
        self.config = config
        self.lock = threading.RLock()
        self.supply_lock = threading.Lock()
        self.health_check_lock = threading.Lock()
        self.pool_refresh_lock = threading.Lock()
        self.health_schedule_wakeup = threading.Event()
        self.pool_snapshot: dict[str, Any] | None = None
        self.sessions: dict[str, dict[str, Any]] = {}
        self.login_attempts: dict[str, dict[str, Any]] = {}
        self._init_database()
        self.settings = self._load_settings()
        self.last_supply: dict[int, datetime] = {}
        self._load_audit()

    def db(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.config.database_file, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _init_database(self) -> None:
        self.config.database_file.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS suppliers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    key_prefix TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS supply_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
                    check_group_id INTEGER NOT NULL,
                    target_group_ids_json TEXT NOT NULL,
                    requested_count INTEGER NOT NULL,
                    submitted_count INTEGER NOT NULL DEFAULT 0,
                    accepted_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS supplied_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES supply_batches(id),
                    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
                    upstream_account_id INTEGER,
                    account_name TEXT,
                    email TEXT,
                    status TEXT NOT NULL,
                    target_group_ids_json TEXT NOT NULL,
                    error_message TEXT,
                    health_alive INTEGER,
                    health_status TEXT,
                    health_checked_at TEXT,
                    health_last_alive_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time TEXT NOT NULL,
                    event TEXT NOT NULL,
                    role TEXT NOT NULL,
                    message TEXT NOT NULL,
                    group_id INTEGER NOT NULL DEFAULT 0,
                    count INTEGER NOT NULL DEFAULT 0,
                    supplier_id INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_supply_batches_supplier ON supply_batches(supplier_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_supplied_accounts_supplier ON supplied_accounts(supplier_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(id DESC);
            """)
            supplier_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(suppliers)").fetchall()}
            if "deleted" not in supplier_columns:
                db.execute("ALTER TABLE suppliers ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
            supplied_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(supplied_accounts)").fetchall()}
            for name, definition in {
                "health_alive": "INTEGER",
                "health_status": "TEXT",
                "health_checked_at": "TEXT",
                "health_last_alive_at": "TEXT",
            }.items():
                if name not in supplied_columns:
                    db.execute(f"ALTER TABLE supplied_accounts ADD COLUMN {name} {definition}")
            db.execute("UPDATE supplied_accounts SET health_last_alive_at=created_at WHERE status='accepted' AND health_last_alive_at IS NULL")

    def _load_settings(self) -> dict[str, Any]:
        with self.db() as db:
            row = db.execute("SELECT value_json FROM app_settings WHERE key='settings'").fetchone()
        if not row:
            data = default_settings()
            with self.db() as db:
                db.execute("INSERT INTO app_settings(key,value_json,updated_at) VALUES('settings',?,?)", (json.dumps(data, ensure_ascii=False), iso_now()))
            return data
        data = json.loads(row["value_json"])
        data.setdefault("global", {}).setdefault("account_health_interval_minutes", 10)
        validate_settings(data)
        return data

    def save_settings(self, value: dict[str, Any]) -> None:
        validate_settings(value)
        value["version"] = 1
        value["updated_at"] = iso_now()
        with self.db() as db:
            db.execute("INSERT INTO app_settings(key,value_json,updated_at) VALUES('settings',?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at", (json.dumps(value, ensure_ascii=False), value["updated_at"]))
        with self.lock:
            self.settings = value
        self.health_schedule_wakeup.set()
        self.append_audit("settings_updated", "admin", "补号策略已更新")

    def settings_snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.settings))

    def _load_audit(self) -> None:
        with self.db() as db:
            rows = db.execute("SELECT group_id, MAX(time) AS time FROM audit_log WHERE event='supply_success' AND group_id>0 GROUP BY group_id").fetchall()
        for row in rows:
            self.last_supply[int(row["group_id"])] = datetime.fromisoformat(str(row["time"]).replace("Z", "+00:00"))

    def append_audit(self, event: str, role: str, message: str, group_id: int = 0, count: int = 0, supplier_id: int | None = None) -> None:
        with self.db() as db:
            db.execute("INSERT INTO audit_log(time,event,role,message,group_id,count,supplier_id) VALUES(?,?,?,?,?,?,?)", (iso_now(), event, role, message, group_id, count, supplier_id))

    def audit_entries(self, limit: int = 200, supplier_id: int = 0, role: str = "", event: str = "", date_from: str = "", date_to: str = "") -> list[dict[str, Any]]:
        clauses, values = [], []
        if supplier_id > 0:
            clauses.append("supplier_id=?"); values.append(supplier_id)
        if role:
            if role not in {"admin", "supplier", "system", "test"}:
                raise ValueError("审计角色筛选无效")
            clauses.append("role=?"); values.append(role)
        if event:
            clauses.append("event LIKE ?"); values.append(f"%{event[:100]}%")
        if date_from:
            clauses.append("date(time)>=date(?)"); values.append(validate_date_filter(date_from))
        if date_to:
            clauses.append("date(time)<=date(?)"); values.append(validate_date_filter(date_to))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.db() as db:
            rows = db.execute(f"SELECT time,event,role,message,group_id,count,supplier_id FROM audit_log{where} ORDER BY id DESC LIMIT ?", (*values, limit)).fetchall()
        return [dict(row) for row in rows]

    def create_session(self, role: str, supplier_id: int | None = None) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(32)
        expires = utc_now() + timedelta(seconds=self.config.session_seconds)
        with self.lock:
            self.sessions[token] = {"role": role, "supplier_id": supplier_id, "expires": expires}
        return token, expires

    def session_role(self, token: str) -> str | None:
        with self.lock:
            session = self.sessions.get(token)
            if not session:
                return None
            if utc_now() >= session["expires"]:
                self.sessions.pop(token, None)
                return None
            role = str(session["role"])
            supplier_id = session.get("supplier_id")
        if role == "supplier":
            with self.db() as db:
                row = db.execute("SELECT enabled,deleted FROM suppliers WHERE id=?", (int(supplier_id or 0),)).fetchone()
            if not row or not bool(row["enabled"]) or bool(row["deleted"]):
                with self.lock:
                    self.sessions.pop(token, None)
                return None
        return role

    def session_context(self, token: str) -> dict[str, Any] | None:
        if not self.session_role(token):
            return None
        with self.lock:
            session = self.sessions.get(token)
            return dict(session) if session else None

    @staticmethod
    def supplier_key_hash(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    def create_supplier(self, name: str) -> dict[str, Any]:
        name = name.strip()
        if not name or len(name) > 100:
            raise ValueError("供应商名称不能为空且最多 100 字")
        raw_key = "sup_" + secrets.token_urlsafe(32)
        now = iso_now()
        with self.db() as db:
            cursor = db.execute("INSERT INTO suppliers(name,key_hash,key_prefix,enabled,created_at) VALUES(?,?,?,?,?)", (name, self.supplier_key_hash(raw_key), raw_key[:12], 1, now))
            supplier_id = int(cursor.lastrowid)
        self.append_audit("supplier_created", "admin", f"创建供应商：{name}", supplier_id=supplier_id)
        return {"id": supplier_id, "name": name, "key": raw_key, "key_prefix": raw_key[:12], "enabled": True, "created_at": now}

    def authenticate_supplier(self, raw_key: str) -> dict[str, Any] | None:
        if not raw_key:
            return None
        digest = self.supplier_key_hash(raw_key)
        with self.db() as db:
            row = db.execute("SELECT id,name,enabled,deleted FROM suppliers WHERE key_hash=?", (digest,)).fetchone()
            if not row or not bool(row["enabled"]) or bool(row["deleted"]):
                return None
            db.execute("UPDATE suppliers SET last_used_at=? WHERE id=?", (iso_now(), int(row["id"])))
        return {"id": int(row["id"]), "name": str(row["name"])}

    def list_suppliers(self) -> list[dict[str, Any]]:
        with self.db() as db:
            rows = db.execute("""
                SELECT s.id,s.name,s.key_prefix,s.enabled,s.created_at,s.last_used_at,
                       COUNT(DISTINCT b.id) batch_count,COALESCE(SUM(b.accepted_count),0) accepted_count
                FROM suppliers s LEFT JOIN supply_batches b ON b.supplier_id=s.id
                WHERE s.deleted=0
                GROUP BY s.id ORDER BY s.id DESC
            """).fetchall()
        return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]

    def update_supplier(self, supplier_id: int, body: dict[str, Any]) -> dict[str, Any]:
        fields, values = [], []
        if "name" in body:
            name = str(body["name"]).strip()
            if not name or len(name) > 100:
                raise ValueError("供应商名称不能为空且最多 100 字")
            fields.append("name=?"); values.append(name)
        if "enabled" in body:
            fields.append("enabled=?"); values.append(1 if body["enabled"] else 0)
        if not fields:
            raise ValueError("没有可更新字段")
        values.append(supplier_id)
        with self.db() as db:
            cursor = db.execute(f"UPDATE suppliers SET {','.join(fields)} WHERE id=? AND deleted=0", values)
            if not cursor.rowcount:
                raise ValueError("供应商不存在")
        self.append_audit("supplier_updated", "admin", f"更新供应商 ID {supplier_id}", supplier_id=supplier_id)
        return {"ok": True}

    def rotate_supplier_key(self, supplier_id: int) -> dict[str, Any]:
        raw_key = "sup_" + secrets.token_urlsafe(32)
        with self.db() as db:
            cursor = db.execute(
                "UPDATE suppliers SET key_hash=?,key_prefix=? WHERE id=? AND deleted=0",
                (self.supplier_key_hash(raw_key), raw_key[:12], supplier_id),
            )
            if not cursor.rowcount:
                raise ValueError("供应商不存在")
        with self.lock:
            for token, session in list(self.sessions.items()):
                if session.get("role") == "supplier" and int(session.get("supplier_id") or 0) == supplier_id:
                    self.sessions.pop(token, None)
        self.append_audit("supplier_key_rotated", "admin", f"重置供应商 ID {supplier_id} 的密钥", supplier_id=supplier_id)
        return {"id": supplier_id, "key": raw_key, "key_prefix": raw_key[:12]}

    def delete_supplier(self, supplier_id: int) -> dict[str, Any]:
        tombstone_hash = self.supplier_key_hash("deleted-" + secrets.token_urlsafe(32))
        with self.db() as db:
            cursor = db.execute("UPDATE suppliers SET enabled=0,deleted=1,key_hash=?,key_prefix='' WHERE id=? AND deleted=0", (tombstone_hash, supplier_id))
            if not cursor.rowcount:
                raise ValueError("供应商不存在或已删除")
        with self.lock:
            for token, session in list(self.sessions.items()):
                if session.get("role") == "supplier" and int(session.get("supplier_id") or 0) == supplier_id:
                    self.sessions.pop(token, None)
        self.append_audit("supplier_deleted", "admin", f"删除供应商 ID {supplier_id}", supplier_id=supplier_id)
        return {"ok": True}

    def supply_history(self, limit: int = 500, supplier_id: int = 0, status: str = "", health: str = "", date_from: str = "", date_to: str = "") -> list[dict[str, Any]]:
        clauses, values = [], []
        if supplier_id > 0:
            clauses.append("a.supplier_id=?"); values.append(supplier_id)
        if status:
            if status not in {"accepted", "rejected"}:
                raise ValueError("补号结果筛选无效")
            clauses.append("a.status=?"); values.append(status)
        if health:
            if health not in {"alive", "unavailable", "unchecked"}:
                raise ValueError("当前验活筛选无效")
            clauses.append({"alive": "a.health_alive=1", "unavailable": "a.health_alive=0", "unchecked": "a.health_alive IS NULL AND a.status='accepted'"}[health])
        if date_from:
            clauses.append("date(a.created_at)>=date(?)"); values.append(validate_date_filter(date_from))
        if date_to:
            clauses.append("date(a.created_at)<=date(?)"); values.append(validate_date_filter(date_to))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.db() as db:
            rows = db.execute(f"""
                SELECT a.id,a.batch_id,a.supplier_id,s.name supplier_name,a.upstream_account_id,
                       a.account_name,a.email,a.status,a.target_group_ids_json,a.error_message,
                       a.health_alive,a.health_status,a.health_checked_at,a.health_last_alive_at,a.created_at
                FROM supplied_accounts a JOIN suppliers s ON s.id=a.supplier_id
                {where} ORDER BY a.id DESC LIMIT ?
            """, (*values, limit)).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["target_group_ids"] = json.loads(item.pop("target_group_ids_json"))
            item["health_alive"] = None if item["health_alive"] is None else bool(item["health_alive"])
            end = iso_now() if item["health_alive"] else item.get("health_last_alive_at")
            item["alive_minutes"] = elapsed_minutes(item["created_at"], end) if item["status"] == "accepted" and end else None
            output.append(item)
        return output

    def check_supplier_accounts_health(self, role: str = "system") -> dict[str, Any]:
        if role not in {"admin", "system", "test"}:
            raise ValueError("验活角色无效")
        with self.health_check_lock:
            _, account_list = self.fetch_pool()
            accounts = {int(item["id"]): item for item in account_list if item.get("id") is not None}
            now = iso_now()
            with self.db() as db:
                rows = db.execute("SELECT id,upstream_account_id FROM supplied_accounts WHERE status='accepted' AND upstream_account_id IS NOT NULL").fetchall()
                alive = unavailable = missing = 0
                for row in rows:
                    account = accounts.get(int(row["upstream_account_id"]))
                    usable = bool(account and is_usable(account))
                    if account is None:
                        health_status = "not_found"
                        missing += 1
                    else:
                        status = str(account.get("status") or "unknown")
                        tier = str(account.get("health_tier") or "").strip()
                        health_status = f"{status} / {tier}" if tier else status
                        if usable:
                            alive += 1
                        else:
                            unavailable += 1
                    db.execute(
                        "UPDATE supplied_accounts SET health_alive=?,health_status=?,health_checked_at=?,health_last_alive_at=CASE WHEN ?=1 THEN ? ELSE health_last_alive_at END WHERE id=?",
                        (1 if usable else 0, health_status, now, 1 if usable else 0, now, int(row["id"])),
                    )
            checked = len(rows)
            self.append_audit(
                "supplier_accounts_health_checked", role,
                f"供应商账号验活：共 {checked}，存活 {alive}，不可用 {unavailable}，未找到 {missing}",
                count=checked,
            )
            return {
                "ok": True, "checked": checked, "alive": alive, "unavailable": unavailable,
                "missing": missing, "checked_at": now, "snapshot_updated_at": self.pool_snapshot_updated_at(),
            }

    def remove_session(self, token: str) -> None:
        with self.lock:
            self.sessions.pop(token, None)

    def login_allowed(self, ip: str) -> bool:
        with self.lock:
            value = self.login_attempts.get(ip)
            return not value or time.time() >= value.get("blocked_until", 0)

    def record_login_failure(self, ip: str) -> None:
        now = time.time()
        with self.lock:
            value = self.login_attempts.get(ip, {"count": 0, "window": now, "blocked_until": 0})
            if now - value["window"] > 300:
                value = {"count": 0, "window": now, "blocked_until": 0}
            value["count"] += 1
            if value["count"] >= 5:
                value["blocked_until"] = now + 600
            self.login_attempts[ip] = value

    def upstream(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.config.base_url + path, data=data, method=method)
        request.add_header("X-Admin-Key", self.config.admin_key)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.config.http_timeout, context=ssl.create_default_context()) as response:
                payload = response.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as error:
            detail = error.read(1000).decode("utf-8", "replace")
            raise RuntimeError(f"Codex2API 返回 {error.code}: {detail[:300]}") from error
        except OSError as error:
            raise RuntimeError(f"Codex2API 请求失败: {error}") from error
        return json.loads(payload) if payload else {}

    def fetch_pool(self, reuse_cache_after_wait: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        with self.pool_refresh_lock:
            if reuse_cache_after_wait:
                with self.lock:
                    snapshot = json.loads(json.dumps(self.pool_snapshot, ensure_ascii=False)) if self.pool_snapshot else None
                if snapshot:
                    return list(snapshot["groups"]), list(snapshot["accounts"])
            results: dict[str, Any] = {}
            errors: list[Exception] = []

            def load(key: str, path: str) -> None:
                try:
                    results[key] = self.upstream("GET", path)
                except Exception as error:  # noqa: BLE001 - surfaced to API
                    errors.append(error)

            threads = [
                threading.Thread(target=load, args=("groups", "/api/admin/account-groups")),
                threading.Thread(target=load, args=("accounts", "/api/admin/accounts")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            if errors:
                raise errors[0]
            groups = list(results["groups"].get("groups", []))
            accounts = list(results["accounts"].get("accounts", []))
            self.store_pool_snapshot(groups, accounts)
            return groups, accounts

    def store_pool_snapshot(self, groups: list[dict[str, Any]], accounts: list[dict[str, Any]]) -> None:
        snapshot = {"groups": groups, "accounts": accounts, "updated_at": iso_now()}
        with self.lock:
            self.pool_snapshot = json.loads(json.dumps(snapshot, ensure_ascii=False))

    def cached_pool(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        with self.lock:
            snapshot = json.loads(json.dumps(self.pool_snapshot, ensure_ascii=False)) if self.pool_snapshot else None
        if snapshot:
            return list(snapshot["groups"]), list(snapshot["accounts"])
        return self.fetch_pool(reuse_cache_after_wait=True)

    def pool_snapshot_updated_at(self) -> str | None:
        with self.lock:
            return str(self.pool_snapshot["updated_at"]) if self.pool_snapshot else None

    def invalidate_pool_snapshot(self) -> None:
        with self.lock:
            self.pool_snapshot = None

    def evaluate(self, groups: list[dict[str, Any]], accounts: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
        policies = {int(item["group_id"]): item for item in settings["groups"]}
        global_settings = settings["global"]
        output: list[dict[str, Any]] = []
        with self.lock:
            last_supply = dict(self.last_supply)
        for group in groups:
            group_id = int(group["id"])
            policy = policies[group_id]
            members = [item for item in accounts if group_id in (item.get("group_ids") or [])]
            usable = sum(1 for item in members if is_usable(item))
            remaining_values = [100 - clamp(float(item["usage_percent_7d"]), 0, 100) for item in members if item.get("usage_percent_7d") is not None]
            remaining = sum(remaining_values) / len(remaining_values) if remaining_values else None
            below_usable = bool(policy["trigger_on_usable"] and usable < policy["min_usable_count"])
            below_remaining = bool(policy["trigger_on_remaining_7d"] and remaining is not None and remaining < policy["min_remaining_7d_percent"])
            conditions = []
            if policy["trigger_on_usable"]:
                conditions.append(below_usable)
            if policy["trigger_on_remaining_7d"]:
                conditions.append(below_remaining)
            triggered = bool(policy["enabled"] and conditions and (all(conditions) if global_settings["trigger_mode"] == "all" else any(conditions)))
            reasons: list[str] = []
            if below_usable:
                reasons.append(f"可用账号 {usable} 低于阈值 {policy['min_usable_count']}")
            if below_remaining and remaining is not None:
                reasons.append(f"7d 剩余额度 {remaining:.1f}% 低于阈值 {policy['min_remaining_7d_percent']:.1f}%")
            if policy["trigger_on_remaining_7d"] and remaining is None:
                reasons.append("暂无 7d 用量样本，未触发额度条件")
            if not policy["enabled"]:
                reasons = ["该分组未启用自动补号策略"]
            count_need = max(0, int(policy["target_usable_count"]) - usable)
            quota_need = 0
            threshold = float(policy["min_remaining_7d_percent"])
            if below_remaining and remaining_values and threshold < 100:
                quota_need = max(0, math.ceil((threshold * len(remaining_values) - sum(remaining_values)) / (100 - threshold)))
            recommended = min(max(count_need, quota_need), int(global_settings["max_accounts_per_run"])) if triggered else 0
            cooldown_until = None
            in_cooldown = False
            if group_id in last_supply:
                end = last_supply[group_id] + timedelta(minutes=int(global_settings["replenish_cooldown_minutes"]))
                if utc_now() < end:
                    in_cooldown = True
                    cooldown_until = end.isoformat().replace("+00:00", "Z")
                    reasons.append("处于补号冷却期")
            output.append({
                "group_id": group_id, "group_name": group.get("name", str(group_id)), "color": group.get("color", "#777"),
                "total_count": len(members), "usable_count": usable, "usage_samples": len(remaining_values),
                "remaining_7d_percent": remaining, "target_usable_count": policy["target_usable_count"],
                "min_usable_count": policy["min_usable_count"], "min_remaining_7d_percent": policy["min_remaining_7d_percent"],
                "below_usable_threshold": below_usable, "below_remaining_7d_threshold": below_remaining,
                "triggered": triggered, "in_cooldown": in_cooldown, "cooldown_until": cooldown_until,
                "recommended_count": recommended,
                "accepting_supply": bool(policy["enabled"] and triggered and not in_cooldown and recommended > 0 and global_settings["supplier_auto_import"]),
                "reasons": reasons, "policy": policy,
            })
        return sorted(output, key=lambda item: item["group_id"])

    def bootstrap(self) -> dict[str, Any]:
        groups, accounts = self.fetch_pool()
        settings = self.settings_snapshot()
        settings["groups"] = reconcile_policies(settings["groups"], groups)
        return {
            "connected": True, "account_count": len(accounts), "groups": groups,
            "settings": settings, "evaluations": self.evaluate(groups, accounts, settings),
            "supplier_auto_import": settings["global"]["supplier_auto_import"],
        }

    def supplier_demand(self) -> dict[str, Any]:
        groups, accounts = self.cached_pool()
        settings = self.settings_snapshot()
        settings["groups"] = reconcile_policies(settings["groups"], groups)
        evaluations = self.evaluate(groups, accounts, settings)
        demands = []
        for item in evaluations:
            if not item["policy"]["enabled"]:
                continue
            if item["accepting_supply"]:
                state, status_text = "accepting", "可提交"
            elif not settings["global"]["supplier_auto_import"]:
                state, status_text = "direct_import_disabled", "管理员未开启直补"
            elif item["in_cooldown"]:
                state, status_text = "cooldown", "冷却中"
            elif not item["triggered"] or item["recommended_count"] <= 0:
                state, status_text = "not_triggered", "未触发补号"
            else:
                state, status_text = "unavailable", "暂不可提交"
            demands.append({
                "group_id": item["group_id"], "group_name": item["group_name"], "color": item["color"],
                "needed": item["recommended_count"], "accepting": item["accepting_supply"],
                "state": state, "status_text": status_text, "cooldown_until": item["cooldown_until"],
                "reasons": item["reasons"], "note": item["policy"].get("supplier_note", ""),
            })
        return {
            "demands": demands, "direct_import_enabled": settings["global"]["supplier_auto_import"],
            "updated_at": self.pool_snapshot_updated_at() or iso_now(), "cached": True,
        }

    def start_monitor(self) -> None:
        def monitor() -> None:
            last_check = 0.0
            last_signature = ""
            while True:
                time.sleep(60)
                settings = self.settings_snapshot()
                interval = max(1, int(settings["global"]["evaluation_interval_minutes"])) * 60
                if time.time() - last_check < interval:
                    continue
                last_check = time.time()
                try:
                    groups, accounts = self.cached_pool()
                    settings["groups"] = reconcile_policies(settings["groups"], groups)
                    active = [(item["group_id"], item["recommended_count"]) for item in self.evaluate(groups, accounts, settings) if item["triggered"]]
                    signature = hashlib.sha256(json.dumps(active).encode()).hexdigest()
                    if signature != last_signature:
                        self.append_audit("demand_changed", "system", "补号需求状态: " + (json.dumps(active, ensure_ascii=False) if active else "无"))
                        last_signature = signature
                except Exception as error:  # noqa: BLE001
                    print(f"monitor warning: {error}")

        threading.Thread(target=monitor, name="pool-demand-monitor", daemon=True).start()

        def health_monitor() -> None:
            run_immediately = True
            while True:
                if not run_immediately:
                    interval_minutes = max(1, int(self.settings_snapshot()["global"]["account_health_interval_minutes"]))
                    wait_seconds = seconds_until_interval_boundary(time.time(), interval_minutes)
                    if self.health_schedule_wakeup.wait(wait_seconds):
                        self.health_schedule_wakeup.clear()
                        continue
                run_immediately = False
                try:
                    self.check_supplier_accounts_health("system")
                except Exception as error:  # noqa: BLE001
                    print(f"supplier health monitor warning: {type(error).__name__}")

        threading.Thread(target=health_monitor, name="supplier-account-health-monitor", daemon=True).start()

    def supply(self, supplier_id: int, group_id: int, token_text: str, proxy_url: str) -> dict[str, Any]:
        with self.supply_lock:
            try:
                return self._supply_locked(supplier_id, group_id, token_text, proxy_url)
            finally:
                # 补号可能新增、删除或改组账号；下一次 demand 必须重新建立快照。
                self.invalidate_pool_snapshot()

    def _supply_locked(self, supplier_id: int, group_id: int, token_text: str, proxy_url: str) -> dict[str, Any]:
        settings = self.settings_snapshot()
        if not settings["global"]["supplier_auto_import"]:
            raise PermissionError("管理员尚未开启供应商直接补号")
        groups, accounts = self.fetch_pool()
        settings["groups"] = reconcile_policies(settings["groups"], groups)
        selected = next((item for item in self.evaluate(groups, accounts, settings) if item["group_id"] == group_id), None)
        if not selected or not selected["accepting_supply"] or selected["recommended_count"] <= 0:
            raise ValueError("该分组当前不需要补号")
        tokens = unique_lines(token_text)
        limit = min(int(selected["recommended_count"]), int(settings["global"]["max_accounts_per_run"]))
        tokens = tokens[:limit]
        if not tokens:
            raise ValueError("没有有效 Token")
        target_group_ids = [int(item) for item in selected["policy"]["target_group_ids"]]
        now = iso_now()
        with self.db() as db:
            cursor = db.execute("INSERT INTO supply_batches(supplier_id,check_group_id,target_group_ids_json,requested_count,status,created_at) VALUES(?,?,?,?,?,?)", (supplier_id, group_id, json.dumps(target_group_ids), len(tokens), "processing", now))
            batch_id = int(cursor.lastrowid)

        accepted = 0
        failed = 0
        results: list[dict[str, Any]] = []
        known_ids = {int(item["id"]) for item in accounts}
        for token in tokens:
            account: dict[str, Any] | None = None
            safe_error = ""
            name = f"supply-{batch_id}-{secrets.token_hex(4)}"
            try:
                upstream = self.upstream("POST", "/api/admin/accounts", {"name": name, "refresh_token": token, "proxy_url": proxy_url.strip()})
                if int(upstream.get("success", 0) or 0) != 1:
                    raise ValueError("Refresh Token 无效或上游拒绝")
                verify_deadline = time.monotonic() + self.config.account_verify_seconds
                while True:
                    payload = self.upstream("GET", "/api/admin/accounts")
                    current = list(payload.get("accounts", []))
                    latest = next((item for item in current if str(item.get("name", "")) == name and ((account is None and int(item["id"]) not in known_ids) or (account is not None and int(item["id"]) == int(account["id"])))), None)
                    if latest:
                        account = latest
                        known_ids.add(int(account["id"]))
                    if account and is_usable(account):
                        break
                    status_value = str(account.get("status", "")).lower() if account else ""
                    terminal = account and (account.get("enabled") is False or status_value in {"banned", "disabled", "error", "expired", "invalid"})
                    remaining_wait = verify_deadline - time.monotonic()
                    if terminal or remaining_wait <= 0:
                        break
                    time.sleep(min(2.0, remaining_wait))
                if not account:
                    raise ValueError("新增后无法确认账号状态")
                if not is_usable(account):
                    raise ValueError(f"账号在验证时限内未存活：{account.get('status') or 'unknown'}")
                self.upstream("PATCH", f"/api/admin/accounts/{int(account['id'])}/scheduler", {"group_ids": target_group_ids})
                # 再读一次确认分组和存活状态，避免把仅“写入成功”的账号计作有效补号。
                payload = self.upstream("GET", "/api/admin/accounts")
                verified = next((item for item in payload.get("accounts", []) if int(item["id"]) == int(account["id"])), None)
                if not verified or not is_usable(verified) or not set(target_group_ids).issubset(set(verified.get("group_ids") or [])):
                    raise ValueError("账号存活或目标分组校验失败")
                account = verified
                accepted += 1
                status = "accepted"
            except Exception as error:  # noqa: BLE001 - detail only goes to server log
                print(f"supplier account validation failed: {type(error).__name__}")
                failed += 1
                status = "rejected"
                safe_error = "Refresh Token 无效、账号不可用或目标分组校验失败"
                if account:
                    try:
                        self.upstream("DELETE", f"/api/admin/accounts/{int(account['id'])}")
                    except Exception as cleanup_error:  # noqa: BLE001
                        print(f"supplier rejected account cleanup failed: {type(cleanup_error).__name__}")
            recorded_at = iso_now()
            with self.db() as db:
                db.execute("""INSERT INTO supplied_accounts(
                                  batch_id,supplier_id,upstream_account_id,account_name,email,status,target_group_ids_json,error_message,
                                  health_alive,health_status,health_checked_at,health_last_alive_at,created_at
                              ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    batch_id, supplier_id, int(account["id"]) if account else None,
                    str(account.get("name", "")) if account else name, str(account.get("email", "")) if account else "",
                    status, json.dumps(target_group_ids), safe_error or None,
                    1 if status == "accepted" else None,
                    str(account.get("status") or "unknown") if account and status == "accepted" else None,
                    recorded_at if status == "accepted" else None,
                    recorded_at if status == "accepted" else None,
                    recorded_at,
                ))
            results.append({"status": status, "account_id": int(account["id"]) if account and status == "accepted" else None})

        with self.db() as db:
            db.execute("UPDATE supply_batches SET submitted_count=?,accepted_count=?,failed_count=?,status=?,completed_at=? WHERE id=?", (len(tokens), accepted, failed, "completed", iso_now(), batch_id))
        if accepted:
            with self.lock:
                self.last_supply[group_id] = utc_now()
            self.append_audit("supply_success", "supplier", "供应商提交并通过存活校验", group_id, accepted, supplier_id)
        if failed:
            self.append_audit("supply_rejected", "supplier", "部分账号未通过存活校验", group_id, failed, supplier_id)
        return {"ok": True, "batch_id": batch_id, "requested": len(tokens), "accepted": accepted, "failed": failed, "needed_before": limit, "group_id": group_id, "results": results}


def validate_settings(data: dict[str, Any]) -> None:
    global_settings = data.get("global") or {}
    bounded(global_settings.get("evaluation_interval_minutes"), 1, 1440, "评估间隔")
    bounded(global_settings.get("account_health_interval_minutes"), 1, 1440, "供应商账号验活间隔")
    bounded(global_settings.get("replenish_cooldown_minutes"), 0, 10080, "补号冷却")
    bounded(global_settings.get("max_accounts_per_run"), 1, 100, "单次补号上限")
    if global_settings.get("trigger_mode") not in {"any", "all"}:
        raise ValueError("条件组合必须是 any 或 all")
    seen: set[int] = set()
    for policy in data.get("groups") or []:
        group_id = int(policy.get("group_id", 0))
        if group_id <= 0 or group_id in seen:
            raise ValueError("分组策略 ID 无效或重复")
        seen.add(group_id)
        bounded(policy.get("target_usable_count"), 0, 10000, "目标可用账号")
        bounded(policy.get("min_usable_count"), 0, int(policy["target_usable_count"]), "可用账号阈值")
        bounded(policy.get("min_remaining_7d_percent"), 0, 100, "7d 剩余额度阈值")
        if len(str(policy.get("supplier_note", ""))) > 500:
            raise ValueError("供应商备注过长")
        targets = policy.get("target_group_ids")
        if not isinstance(targets, list) or not targets or len(targets) > 20:
            raise ValueError("补号目标分组必须包含 1..20 个分组")
        if any(int(item) <= 0 for item in targets) or len({int(item) for item in targets}) != len(targets):
            raise ValueError("补号目标分组 ID 无效或重复")


def bounded(value: Any, low: float, high: float, label: str) -> None:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label}不是有效数字") from error
    if number < low or number > high:
        raise ValueError(f"{label}必须在 {low:g}..{high:g} 范围")


def validate_date_filter(value: str) -> str:
    value = value.strip()
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError("日期筛选必须是 YYYY-MM-DD") from error
    return value


def elapsed_minutes(start: str, end: str) -> int:
    try:
        start_time = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        end_time = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        return max(0, int((end_time - start_time).total_seconds() // 60))
    except (TypeError, ValueError):
        return 0


def seconds_until_interval_boundary(timestamp: float, interval_minutes: int) -> float:
    interval = max(1, int(interval_minutes)) * 60
    remainder = timestamp % interval
    return float(interval if remainder < 0.001 else interval - remainder)


def reconcile_policies(existing: list[dict[str, Any]], groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {int(item["group_id"]): item for item in existing}
    output = []
    for group in groups:
        group_id = int(group["id"])
        if group_id in by_id:
            policy = by_id[group_id]
            policy.setdefault("target_group_ids", [group_id])
            output.append(policy)
            continue
        target = max(int(group.get("member_count") or 0), 10)
        output.append({
            "group_id": group_id, "enabled": False, "target_usable_count": target,
            "min_usable_count": max(1, math.ceil(target * 0.8)), "min_remaining_7d_percent": 25,
            "trigger_on_usable": True, "trigger_on_remaining_7d": True,
            "target_group_ids": [group_id], "supplier_note": "",
        })
    return output


def is_usable(account: dict[str, Any]) -> bool:
    if account.get("enabled") is False:
        return False
    if str(account.get("status", "")).lower() not in {"active", "ready"}:
        return False
    return str(account.get("health_tier", "")).lower() not in {"banned", "error"}


def unique_lines(value: str) -> list[str]:
    return list(dict.fromkeys(line.strip() for line in value.replace("\r\n", "\n").split("\n") if line.strip()))


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class Handler(BaseHTTPRequestHandler):
    manager: Manager
    server_version = "AccountPoolManager/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.log_date_time_string()} {self.client_address[0]} {fmt % args}")

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            return self.json_response(200, {"ok": True})
        if path == "/api/admin/bootstrap":
            return self.protected("admin", lambda: self.manager.bootstrap())
        if path == "/api/admin/audit":
            return self.protected("admin", lambda: {"entries": self.manager.audit_entries(
                supplier_id=self.query_int("supplier_id"), role=self.query_value("role"), event=self.query_value("event"),
                date_from=self.query_value("date_from"), date_to=self.query_value("date_to"),
            )})
        if path == "/api/admin/suppliers":
            return self.protected("admin", lambda: {"suppliers": self.manager.list_suppliers()})
        if path == "/api/admin/supplies":
            return self.protected("admin", lambda: {"accounts": self.manager.supply_history(
                supplier_id=self.query_int("supplier_id"), status=self.query_value("status"), health=self.query_value("health"),
                date_from=self.query_value("date_from"), date_to=self.query_value("date_to"),
            )})
        if path == "/api/supplier/demand":
            return self.protected("supplier", lambda: self.manager.supplier_demand())
        if path == "/api/supplier/v1/demand":
            return self.supplier_api(lambda supplier: self.manager.supplier_demand())
        self.serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if not self.valid_origin():
            return self.json_response(403, {"error": "来源校验失败"})
        if path in {"/api/auth/login", "/api/supplier/auth/login"}:
            return self.login("supplier" if "/supplier/" in path else "admin")
        if path == "/api/auth/logout":
            token = self.session_token()
            if token:
                self.manager.remove_session(token)
            self.send_response(200)
            self.security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", self.expired_cookie())
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return
        if path == "/api/admin/suppliers":
            return self.protected("admin", lambda: self.manager.create_supplier(str(self.read_json(8192).get("name", ""))))
        if path == "/api/admin/supplies/health-check":
            return self.protected("admin", lambda: self.manager.check_supplier_accounts_health("admin"))
        prefix = "/api/admin/suppliers/"
        suffix = "/rotate-key"
        if path.startswith(prefix) and path.endswith(suffix) and path[len(prefix):-len(suffix)].isdigit():
            supplier_id = int(path[len(prefix):-len(suffix)])
            return self.protected("admin", lambda: self.manager.rotate_supplier_key(supplier_id))
        if path == "/api/supplier/supply":
            def supply() -> dict[str, Any]:
                body = self.read_json(2 * 1024 * 1024)
                context = self.manager.session_context(self.session_token()) or {}
                return self.manager.supply(int(context.get("supplier_id") or 0), int(body.get("group_id", 0)), str(body.get("tokens", "")), str(body.get("proxy_url", "")))
            return self.protected("supplier", supply)
        if path == "/api/supplier/v1/supply":
            def api_supply(supplier: dict[str, Any]) -> dict[str, Any]:
                body = self.read_json(2 * 1024 * 1024)
                return self.manager.supply(int(supplier["id"]), int(body.get("group_id", 0)), str(body.get("refresh_tokens", body.get("tokens", ""))), str(body.get("proxy_url", "")))
            return self.supplier_api(api_supply)
        self.json_response(404, {"error": "not found"})

    def do_PUT(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if not self.valid_origin():
            return self.json_response(403, {"error": "来源校验失败"})
        if path == "/api/admin/settings":
            def save() -> dict[str, Any]:
                value = self.read_json(256 * 1024)
                self.manager.save_settings(value)
                return {"ok": True, "settings": value}
            return self.protected("admin", save)
        self.json_response(404, {"error": "not found"})

    def do_PATCH(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if not self.valid_origin():
            return self.json_response(403, {"error": "来源校验失败"})
        prefix = "/api/admin/suppliers/"
        if path.startswith(prefix) and path[len(prefix):].isdigit():
            supplier_id = int(path[len(prefix):])
            return self.protected("admin", lambda: self.manager.update_supplier(supplier_id, self.read_json(8192)))
        self.json_response(404, {"error": "not found"})

    def do_DELETE(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if not self.valid_origin():
            return self.json_response(403, {"error": "来源校验失败"})
        prefix = "/api/admin/suppliers/"
        if path.startswith(prefix) and path[len(prefix):].isdigit():
            supplier_id = int(path[len(prefix):])
            return self.protected("admin", lambda: self.manager.delete_supplier(supplier_id))
        self.json_response(404, {"error": "not found"})

    def login(self, role: str) -> None:
        ip = self.client_address[0]
        if not self.manager.login_allowed(ip):
            return self.json_response(429, {"error": "尝试次数过多，请稍后再试"})
        try:
            password = str(self.read_json(4096).get("password", ""))
        except ValueError:
            return self.json_response(400, {"error": "请求格式错误"})
        supplier = self.manager.authenticate_supplier(password) if role == "supplier" else None
        valid = bool(supplier) if role == "supplier" else hmac.compare_digest(password, self.manager.config.admin_password)
        if not valid:
            self.manager.record_login_failure(ip)
            return self.json_response(401, {"error": "密码错误"})
        with self.manager.lock:
            self.manager.login_attempts.pop(ip, None)
        token, expires = self.manager.create_session(role, int(supplier["id"]) if supplier else None)
        self.manager.append_audit("login", role, f"{role} 登录成功", supplier_id=int(supplier["id"]) if supplier else None)
        self.send_response(200)
        self.security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie", self.session_cookie(token, expires))
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "role": role, "expires_at": expires.isoformat()}).encode())

    def protected(self, role: str, action: Any) -> None:
        token = self.session_token()
        if not token or self.manager.session_role(token) != role:
            return self.json_response(401, {"error": "请先登录或会话已过期"})
        try:
            self.json_response(200, action())
        except PermissionError as error:
            self.json_response(403, {"error": str(error)})
        except ValueError as error:
            self.json_response(409, {"error": str(error)})
        except RuntimeError as error:
            self.json_response(502, {"error": str(error)})
        except Exception as error:  # noqa: BLE001
            self.json_response(500, {"error": f"服务内部错误: {error}"})

    def supplier_api(self, action: Any) -> None:
        raw_key = self.headers.get("X-Supplier-Key", "").strip()
        supplier = self.manager.authenticate_supplier(raw_key)
        if not supplier:
            return self.json_response(401, {"error": "供应商密钥无效或已禁用"})
        try:
            self.json_response(200, action(supplier))
        except PermissionError as error:
            self.json_response(403, {"error": str(error)})
        except ValueError as error:
            self.json_response(409, {"error": str(error)})
        except RuntimeError:
            self.json_response(502, {"error": "上游服务暂时不可用"})
        except Exception as error:  # noqa: BLE001
            print(f"supplier api error: {error}")
            self.json_response(500, {"error": "服务内部错误"})

    def read_json(self, limit: int) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Content-Length 无效") from error
        if length <= 0 or length > limit:
            raise ValueError("请求体为空或过大")
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as error:
            raise ValueError("JSON 格式错误") from error
        if not isinstance(value, dict):
            raise ValueError("请求体必须是对象")
        return value

    def query_value(self, name: str) -> str:
        values = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get(name, [])
        return str(values[0]).strip() if values else ""

    def query_int(self, name: str) -> int:
        value = self.query_value(name)
        if not value:
            return 0
        if not value.isdigit():
            raise ValueError(f"{name} 必须是正整数")
        return int(value)

    def session_token(self) -> str:
        cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("pool_manager_session")
        return morsel.value if morsel else ""

    def valid_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urllib.parse.urlsplit(origin).netloc.lower() == self.headers.get("Host", "").lower()

    def session_cookie(self, token: str, expires: datetime) -> str:
        parts = [f"pool_manager_session={token}", "Path=/", "HttpOnly", "SameSite=Strict", f"Max-Age={self.manager.config.session_seconds}", f"Expires={expires.strftime('%a, %d %b %Y %H:%M:%S GMT')}"]
        if self.manager.config.secure_cookie:
            parts.append("Secure")
        return "; ".join(parts)

    def expired_cookie(self) -> str:
        return "pool_manager_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"

    def json_response(self, status: int, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        candidate = (WEB_ROOT / relative).resolve()
        if WEB_ROOT.resolve() not in candidate.parents and candidate != WEB_ROOT.resolve():
            return self.json_response(404, {"error": "not found"})
        if not candidate.is_file():
            candidate = WEB_ROOT / "index.html"
        content_type = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}.get(candidate.suffix, "application/octet-stream")
        payload = candidate.read_bytes()
        self.send_response(200)
        self.security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


def main() -> None:
    config = Config.from_env()
    manager = Manager(config)
    manager.start_monitor()
    Handler.manager = manager
    server = ThreadingHTTPServer((config.host, config.port), Handler)
    print(f"Account Pool Manager: http://{config.host}:{config.port}")
    print("管理密钥仅保存在服务端环境变量中。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
