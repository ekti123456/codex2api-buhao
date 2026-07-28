from __future__ import annotations

import hashlib
import hmac
import http.cookies
import json
import math
import os
import secrets
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
    supplier_password: str
    settings_file: Path
    audit_file: Path
    secure_cookie: bool
    session_seconds: int
    http_timeout: int

    @classmethod
    def from_env(cls) -> "Config":
        required = {
            "CODEX2API_BASE_URL": os.getenv("CODEX2API_BASE_URL", "").strip().rstrip("/"),
            "CODEX2API_ADMIN_KEY": os.getenv("CODEX2API_ADMIN_KEY", "").strip(),
            "POOL_MANAGER_ADMIN_PASSWORD": os.getenv("POOL_MANAGER_ADMIN_PASSWORD", ""),
            "POOL_MANAGER_SUPPLIER_PASSWORD": os.getenv("POOL_MANAGER_SUPPLIER_PASSWORD", ""),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError("缺少环境变量: " + ", ".join(missing))
        if hmac.compare_digest(required["POOL_MANAGER_ADMIN_PASSWORD"], required["POOL_MANAGER_SUPPLIER_PASSWORD"]):
            raise RuntimeError("管理员密码和供应商密码不能相同")
        upstream_url = urllib.parse.urlsplit(required["CODEX2API_BASE_URL"])
        if upstream_url.scheme not in {"http", "https"} or not upstream_url.netloc:
            raise RuntimeError("CODEX2API_BASE_URL 必须是完整的 http/https 地址")
        return cls(
            host=os.getenv("POOL_MANAGER_HOST", "127.0.0.1"),
            port=int(os.getenv("POOL_MANAGER_PORT", "8790")),
            base_url=required["CODEX2API_BASE_URL"],
            admin_key=required["CODEX2API_ADMIN_KEY"],
            admin_password=required["POOL_MANAGER_ADMIN_PASSWORD"],
            supplier_password=required["POOL_MANAGER_SUPPLIER_PASSWORD"],
            settings_file=Path(os.getenv("POOL_MANAGER_SETTINGS_FILE", str(ROOT / "data" / "settings.json"))),
            audit_file=Path(os.getenv("POOL_MANAGER_AUDIT_FILE", str(ROOT / "data" / "audit.jsonl"))),
            secure_cookie=os.getenv("POOL_MANAGER_SECURE_COOKIE", "false").lower() == "true",
            session_seconds=int(os.getenv("POOL_MANAGER_SESSION_HOURS", "12")) * 3600,
            http_timeout=int(os.getenv("POOL_MANAGER_HTTP_TIMEOUT_SECONDS", "30")),
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def default_settings() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": iso_now(),
        "global": {
            "evaluation_interval_minutes": 5,
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
        self.sessions: dict[str, dict[str, Any]] = {}
        self.login_attempts: dict[str, dict[str, Any]] = {}
        self.settings = self._load_settings()
        self.audit: list[dict[str, Any]] = []
        self.last_supply: dict[int, datetime] = {}
        self._load_audit()

    def _load_settings(self) -> dict[str, Any]:
        if not self.config.settings_file.exists():
            return default_settings()
        data = json.loads(self.config.settings_file.read_text("utf-8"))
        validate_settings(data)
        return data

    def save_settings(self, value: dict[str, Any]) -> None:
        validate_settings(value)
        value["version"] = 1
        value["updated_at"] = iso_now()
        self.config.settings_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config.settings_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, self.config.settings_file)
        with self.lock:
            self.settings = value
        self.append_audit("settings_updated", "admin", "补号策略已更新")

    def settings_snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.settings))

    def _load_audit(self) -> None:
        if not self.config.audit_file.exists():
            return
        for line in self.config.audit_file.read_text("utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.audit.append(entry)
            if entry.get("event") == "supply_success" and entry.get("group_id"):
                self.last_supply[int(entry["group_id"])] = datetime.fromisoformat(entry["time"].replace("Z", "+00:00"))
        self.audit = self.audit[-500:]

    def append_audit(self, event: str, role: str, message: str, group_id: int = 0, count: int = 0) -> None:
        entry = {"time": iso_now(), "event": event, "role": role, "message": message, "group_id": group_id, "count": count}
        with self.lock:
            self.audit.append(entry)
            self.audit = self.audit[-500:]
        self.config.audit_file.parent.mkdir(parents=True, exist_ok=True)
        with self.config.audit_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def create_session(self, role: str) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(32)
        expires = utc_now() + timedelta(seconds=self.config.session_seconds)
        with self.lock:
            self.sessions[token] = {"role": role, "expires": expires}
        return token, expires

    def session_role(self, token: str) -> str | None:
        with self.lock:
            session = self.sessions.get(token)
            if not session:
                return None
            if utc_now() >= session["expires"]:
                self.sessions.pop(token, None)
                return None
            return str(session["role"])

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

    def fetch_pool(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
        return list(results["groups"].get("groups", [])), list(results["accounts"].get("accounts", []))

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
            "connected": True, "base_url": self.config.base_url, "account_count": len(accounts), "groups": groups,
            "settings": settings, "evaluations": self.evaluate(groups, accounts, settings),
            "supplier_auto_import": settings["global"]["supplier_auto_import"],
        }

    def supplier_demand(self) -> dict[str, Any]:
        groups, accounts = self.fetch_pool()
        settings = self.settings_snapshot()
        settings["groups"] = reconcile_policies(settings["groups"], groups)
        evaluations = self.evaluate(groups, accounts, settings)
        demands = [{
            "group_id": item["group_id"], "group_name": item["group_name"], "color": item["color"],
            "needed": item["recommended_count"], "accepting": item["accepting_supply"], "reasons": item["reasons"],
            "note": item["policy"].get("supplier_note", ""),
        } for item in evaluations if item["policy"]["enabled"]]
        return {"demands": demands, "direct_import_enabled": settings["global"]["supplier_auto_import"], "updated_at": iso_now()}

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
                    groups, accounts = self.fetch_pool()
                    settings["groups"] = reconcile_policies(settings["groups"], groups)
                    active = [(item["group_id"], item["recommended_count"]) for item in self.evaluate(groups, accounts, settings) if item["triggered"]]
                    signature = hashlib.sha256(json.dumps(active).encode()).hexdigest()
                    if signature != last_signature:
                        self.append_audit("demand_changed", "system", "补号需求状态: " + (json.dumps(active, ensure_ascii=False) if active else "无"))
                        last_signature = signature
                except Exception as error:  # noqa: BLE001
                    print(f"monitor warning: {error}")

        threading.Thread(target=monitor, name="pool-demand-monitor", daemon=True).start()

    def supply(self, group_id: int, token_text: str, proxy_url: str) -> dict[str, Any]:
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
        before = {int(item["id"]) for item in accounts}
        name = f"supplier-{group_id}-{utc_now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
        payload = {"name": name, "refresh_token": "\n".join(tokens), "proxy_url": proxy_url.strip(), "group_ids": [group_id]}
        try:
            upstream = self.upstream("POST", "/api/admin/accounts", payload)
            if not upstream.get("bound_groups") and int(upstream.get("success", len(tokens)) or 0) > 0:
                self._fallback_bind_new_accounts(before, group_id, name)
        except Exception as error:
            self.append_audit("supply_failed", "supplier", "上游新增失败", group_id, len(tokens))
            print(f"supplier upstream error: {error}")
            raise RuntimeError("上游新增失败，请联系管理员检查服务日志") from error
        with self.lock:
            self.last_supply[group_id] = utc_now()
        self.append_audit("supply_success", "supplier", "供应商补号成功", group_id, len(tokens))
        return {"ok": True, "submitted": len(tokens), "group_id": group_id, "message": str(upstream.get("message", ""))}

    def _fallback_bind_new_accounts(self, before: set[int], group_id: int, name_prefix: str) -> None:
        _, accounts = self.fetch_pool()
        new_accounts = [item for item in accounts if int(item["id"]) not in before and str(item.get("name", "")).startswith(name_prefix) and group_id not in (item.get("group_ids") or [])]
        if not new_accounts:
            raise RuntimeError("无法定位本次新增账号")
        for item in new_accounts:
            self.upstream("PATCH", f"/api/admin/accounts/{int(item['id'])}/scheduler", {"group_ids": [group_id]})


def validate_settings(data: dict[str, Any]) -> None:
    global_settings = data.get("global") or {}
    bounded(global_settings.get("evaluation_interval_minutes"), 1, 1440, "评估间隔")
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


def bounded(value: Any, low: float, high: float, label: str) -> None:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label}不是有效数字") from error
    if number < low or number > high:
        raise ValueError(f"{label}必须在 {low:g}..{high:g} 范围")


def reconcile_policies(existing: list[dict[str, Any]], groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {int(item["group_id"]): item for item in existing}
    output = []
    for group in groups:
        group_id = int(group["id"])
        if group_id in by_id:
            output.append(by_id[group_id])
            continue
        target = max(int(group.get("member_count") or 0), 10)
        output.append({
            "group_id": group_id, "enabled": False, "target_usable_count": target,
            "min_usable_count": max(1, math.ceil(target * 0.8)), "min_remaining_7d_percent": 25,
            "trigger_on_usable": True, "trigger_on_remaining_7d": True, "supplier_note": "",
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
            def audit() -> dict[str, Any]:
                with self.manager.lock:
                    return {"entries": list(reversed(self.manager.audit[-100:]))}
            return self.protected("admin", audit)
        if path == "/api/supplier/demand":
            return self.protected("supplier", lambda: self.manager.supplier_demand())
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
        if path == "/api/supplier/supply":
            def supply() -> dict[str, Any]:
                body = self.read_json(2 * 1024 * 1024)
                return self.manager.supply(int(body.get("group_id", 0)), str(body.get("tokens", "")), str(body.get("proxy_url", "")))
            return self.protected("supplier", supply)
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

    def login(self, role: str) -> None:
        ip = self.client_address[0]
        if not self.manager.login_allowed(ip):
            return self.json_response(429, {"error": "尝试次数过多，请稍后再试"})
        try:
            password = str(self.read_json(4096).get("password", ""))
        except ValueError:
            return self.json_response(400, {"error": "请求格式错误"})
        expected = self.manager.config.supplier_password if role == "supplier" else self.manager.config.admin_password
        if not hmac.compare_digest(password, expected):
            self.manager.record_login_failure(ip)
            return self.json_response(401, {"error": "密码错误"})
        with self.manager.lock:
            self.manager.login_attempts.pop(ip, None)
        token, expires = self.manager.create_session(role)
        self.manager.append_audit("login", role, f"{role} 登录成功")
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
        self.send_header("Cache-Control", "no-store" if candidate.suffix == ".html" else "public, max-age=3600")
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
