import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch
from pathlib import Path

from server import Config, Manager, configured_port, default_settings, is_usable, reconcile_policies, seconds_until_interval_boundary, utc_now, validate_settings


class ManagerLogicTests(unittest.TestCase):
    def make_manager(self) -> Manager:
        temp = Path(tempfile.mkdtemp())
        config = Config(
            host="127.0.0.1", port=0, base_url="https://example.invalid", admin_key="secret",
            admin_password="admin-password", database_file=temp / "pool.sqlite3",
            secure_cookie=False, session_seconds=3600, http_timeout=1, account_verify_seconds=5,
        )
        return Manager(config)

    def test_usable_account_rules(self):
        self.assertTrue(is_usable({"status": "active", "health_tier": "healthy"}))
        self.assertTrue(is_usable({"status": "ready"}))
        self.assertFalse(is_usable({"status": "cooldown"}))
        self.assertFalse(is_usable({"status": "active", "enabled": False}))
        self.assertFalse(is_usable({"status": "active", "health_tier": "banned"}))

    def test_platform_port_variables_are_supported(self):
        with patch.dict("os.environ", {"PORT": "${WEB_PORT}", "WEB_PORT": "9123", "POOL_MANAGER_PORT": "8790"}, clear=True):
            self.assertEqual(9123, configured_port())
        with patch.dict("os.environ", {"PORT": "9234", "POOL_MANAGER_PORT": "8790"}, clear=True):
            self.assertEqual(9234, configured_port())

    def test_health_schedule_uses_exact_interval_boundaries(self):
        self.assertEqual(599, seconds_until_interval_boundary(601, 10))
        self.assertEqual(600, seconds_until_interval_boundary(600, 10))

    def test_reconcile_creates_safe_disabled_policy(self):
        policies = reconcile_policies([], [{"id": 3, "name": "PRO", "member_count": 7}])
        self.assertEqual(10, policies[0]["target_usable_count"])
        self.assertEqual(8, policies[0]["min_usable_count"])
        self.assertFalse(policies[0]["enabled"])

    def test_count_trigger_recommends_target_gap(self):
        manager = self.make_manager()
        settings = default_settings()
        settings["groups"] = [{
            "group_id": 3, "enabled": True, "target_usable_count": 10, "min_usable_count": 8,
            "min_remaining_7d_percent": 25, "trigger_on_usable": True,
            "trigger_on_remaining_7d": False, "target_group_ids": [3], "supplier_note": "",
        }]
        groups = [{"id": 3, "name": "PRO", "color": "#fff"}]
        accounts = [{"id": i, "status": "active", "group_ids": [3], "usage_percent_7d": 10} for i in range(7)]
        result = manager.evaluate(groups, accounts, settings)[0]
        self.assertTrue(result["triggered"])
        self.assertEqual(3, result["recommended_count"])

    def test_quota_trigger_estimates_capacity_accounts(self):
        manager = self.make_manager()
        settings = default_settings()
        settings["groups"] = [{
            "group_id": 4, "enabled": True, "target_usable_count": 2, "min_usable_count": 1,
            "min_remaining_7d_percent": 50, "trigger_on_usable": False,
            "trigger_on_remaining_7d": True, "target_group_ids": [4], "supplier_note": "",
        }]
        groups = [{"id": 4, "name": "PLUS", "color": "#fff"}]
        accounts = [
            {"id": 1, "status": "active", "group_ids": [4], "usage_percent_7d": 90},
            {"id": 2, "status": "active", "group_ids": [4], "usage_percent_7d": 90},
        ]
        result = manager.evaluate(groups, accounts, settings)[0]
        self.assertEqual(10.0, result["remaining_7d_percent"])
        self.assertEqual(2, result["recommended_count"])

    def test_invalid_threshold_is_rejected(self):
        settings = default_settings()
        settings["groups"] = [{
            "group_id": 1, "enabled": True, "target_usable_count": 5, "min_usable_count": 6,
            "min_remaining_7d_percent": 20, "trigger_on_usable": True,
            "trigger_on_remaining_7d": True, "target_group_ids": [1], "supplier_note": "",
        }]
        with self.assertRaises(ValueError):
            validate_settings(settings)

    def test_supplier_keys_are_hashed_and_can_be_disabled(self):
        manager = self.make_manager()
        created = manager.create_supplier("供应商 A")
        self.assertTrue(created["key"].startswith("sup_"))
        self.assertEqual(created["id"], manager.authenticate_supplier(created["key"])["id"])
        with manager.db() as db:
            stored = db.execute("SELECT key_hash FROM suppliers WHERE id=?", (created["id"],)).fetchone()[0]
        self.assertNotEqual(created["key"], stored)
        token, _ = manager.create_session("supplier", created["id"])
        self.assertEqual("supplier", manager.session_role(token))
        manager.update_supplier(created["id"], {"enabled": False})
        self.assertIsNone(manager.authenticate_supplier(created["key"]))
        self.assertIsNone(manager.session_role(token))
        manager.delete_supplier(created["id"])
        self.assertEqual([], manager.list_suppliers())

    def test_supplier_key_can_be_rotated_without_storing_plaintext(self):
        manager = self.make_manager()
        created = manager.create_supplier("供应商密钥轮换")
        token, _ = manager.create_session("supplier", created["id"])
        rotated = manager.rotate_supplier_key(created["id"])
        self.assertNotEqual(created["key"], rotated["key"])
        self.assertIsNone(manager.authenticate_supplier(created["key"]))
        self.assertEqual(created["id"], manager.authenticate_supplier(rotated["key"])["id"])
        self.assertIsNone(manager.session_role(token))
        with manager.db() as db:
            stored = db.execute("SELECT key_hash,key_prefix FROM suppliers WHERE id=?", (created["id"],)).fetchone()
        self.assertNotEqual(rotated["key"], stored["key_hash"])
        self.assertEqual(rotated["key"][:12], stored["key_prefix"])

    def test_supply_and_audit_history_filters(self):
        manager = self.make_manager()
        first = manager.create_supplier("供应商甲")
        second = manager.create_supplier("供应商乙")
        with manager.db() as db:
            first_batch = db.execute(
                "INSERT INTO supply_batches(supplier_id,check_group_id,target_group_ids_json,requested_count,submitted_count,accepted_count,failed_count,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (first["id"], 1, "[1]", 1, 1, 1, 0, "completed", "2026-07-27T02:00:00Z"),
            ).lastrowid
            second_batch = db.execute(
                "INSERT INTO supply_batches(supplier_id,check_group_id,target_group_ids_json,requested_count,submitted_count,accepted_count,failed_count,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (second["id"], 1, "[1]", 1, 1, 0, 1, "completed", "2026-07-28T02:00:00Z"),
            ).lastrowid
            db.execute(
                "INSERT INTO supplied_accounts(batch_id,supplier_id,status,target_group_ids_json,created_at) VALUES(?,?,?,?,?)",
                (first_batch, first["id"], "accepted", "[1]", "2026-07-27T02:00:00Z"),
            )
            db.execute(
                "INSERT INTO supplied_accounts(batch_id,supplier_id,status,target_group_ids_json,created_at) VALUES(?,?,?,?,?)",
                (second_batch, second["id"], "rejected", "[1]", "2026-07-28T02:00:00Z"),
            )
        manager.append_audit("supply_success", "supplier", "甲补号成功", supplier_id=first["id"])
        manager.append_audit("supplier_updated", "admin", "更新乙", supplier_id=second["id"])
        with manager.db() as db:
            db.execute("UPDATE audit_log SET time='2026-07-27T03:00:00Z' WHERE supplier_id=?", (first["id"],))
            db.execute("UPDATE audit_log SET time='2026-07-28T03:00:00Z' WHERE supplier_id=?", (second["id"],))

        supplied = manager.supply_history(supplier_id=first["id"], status="accepted", date_from="2026-07-27", date_to="2026-07-27")
        self.assertEqual(1, len(supplied))
        self.assertEqual("供应商甲", supplied[0]["supplier_name"])
        audited = manager.audit_entries(supplier_id=first["id"], role="supplier", event="success", date_from="2026-07-27", date_to="2026-07-27")
        self.assertEqual(1, len(audited))
        self.assertEqual("supply_success", audited[0]["event"])

    def test_supplier_account_health_check_lists_upstream_once(self):
        manager = self.make_manager()
        supplier = manager.create_supplier("验活供应商")
        created_at = (utc_now() - timedelta(minutes=65)).isoformat().replace("+00:00", "Z")
        with manager.db() as db:
            batch_id = db.execute(
                "INSERT INTO supply_batches(supplier_id,check_group_id,target_group_ids_json,requested_count,status,created_at) VALUES(?,?,?,?,?,?)",
                (supplier["id"], 1, "[1]", 3, "completed", created_at),
            ).lastrowid
            for account_id in (11, 12, 13):
                db.execute(
                    "INSERT INTO supplied_accounts(batch_id,supplier_id,upstream_account_id,status,target_group_ids_json,created_at) VALUES(?,?,?,?,?,?)",
                    (batch_id, supplier["id"], account_id, "accepted", "[1]", created_at),
                )
        calls = []

        def upstream(method, path, body=None):
            calls.append((method, path))
            if path == "/api/admin/account-groups":
                return {"groups": [{"id": 1, "name": "PLUS"}]}
            return {"accounts": [
                {"id": 11, "status": "active", "health_tier": "healthy", "enabled": True, "locked": True},
                {"id": 12, "status": "active", "enabled": False},
            ]}

        manager.upstream = upstream
        result = manager.check_supplier_accounts_health("test")
        self.assertEqual({"checked": 3, "alive": 1, "unavailable": 1, "missing": 1}, {key: result[key] for key in ("checked", "alive", "unavailable", "missing")})
        self.assertEqual(1, calls.count(("GET", "/api/admin/account-groups")))
        self.assertEqual(1, calls.count(("GET", "/api/admin/accounts")))
        alive = manager.supply_history(health="alive")
        self.assertEqual([11], [item["upstream_account_id"] for item in alive])
        self.assertGreaterEqual(alive[0]["alive_minutes"], 65)
        self.assertTrue(alive[0]["health_alive"])
        unavailable = manager.supply_history(health="unavailable")
        self.assertEqual({12, 13}, {item["upstream_account_id"] for item in unavailable})

    def test_supplier_demand_uses_health_snapshot_cache(self):
        manager = self.make_manager()
        settings = default_settings()
        settings["global"]["supplier_auto_import"] = True
        settings["groups"] = [{
            "group_id": 1, "enabled": True, "target_usable_count": 2, "min_usable_count": 2,
            "min_remaining_7d_percent": 25, "trigger_on_usable": True,
            "trigger_on_remaining_7d": False, "target_group_ids": [1], "supplier_note": "",
        }]
        manager.save_settings(settings)
        calls = []
        upstream_accounts = []

        def upstream(method, path, body=None):
            calls.append((method, path))
            if path == "/api/admin/account-groups":
                return {"groups": [{"id": 1, "name": "PLUS"}]}
            return {"accounts": [dict(item) for item in upstream_accounts]}

        manager.upstream = upstream
        first = manager.supplier_demand()
        self.assertEqual(2, first["demands"][0]["needed"])
        upstream_accounts.extend([
            {"id": 1, "status": "active", "group_ids": [1]},
            {"id": 2, "status": "active", "group_ids": [1]},
        ])
        second = manager.supplier_demand()
        self.assertEqual(2, second["demands"][0]["needed"])
        self.assertEqual(first["updated_at"], second["updated_at"])
        self.assertEqual(1, calls.count(("GET", "/api/admin/accounts")))

        manager.check_supplier_accounts_health("test")
        after_refresh_calls = len(calls)
        third = manager.supplier_demand()
        self.assertEqual(0, third["demands"][0]["needed"])
        self.assertEqual(after_refresh_calls, len(calls))

    def test_supplier_demand_explains_cooldown_without_exposing_target_groups(self):
        manager = self.make_manager()
        settings = default_settings()
        settings["global"]["supplier_auto_import"] = True
        settings["groups"] = [{
            "group_id": 4, "enabled": True, "target_usable_count": 10, "min_usable_count": 8,
            "min_remaining_7d_percent": 25, "trigger_on_usable": True,
            "trigger_on_remaining_7d": False, "target_group_ids": [4, 5], "supplier_note": "",
        }]
        manager.save_settings(settings)
        manager.last_supply[4] = utc_now()
        manager.cached_pool = lambda: ([{"id": 4, "name": "PLUS池"}, {"id": 5, "name": "分流分组"}], [])
        demand = manager.supplier_demand()["demands"][0]
        self.assertEqual("cooldown", demand["state"])
        self.assertEqual("冷却中", demand["status_text"])
        self.assertNotIn("target_group_ids", demand)
        self.assertNotIn("target_group_names", demand)

    def test_supply_checks_one_group_and_binds_multiple_targets(self):
        manager = self.make_manager()
        supplier = manager.create_supplier("供应商 B")
        settings = default_settings()
        settings["global"]["supplier_auto_import"] = True
        settings["groups"] = [{
            "group_id": 1, "enabled": True, "target_usable_count": 2, "min_usable_count": 2,
            "min_remaining_7d_percent": 25, "trigger_on_usable": True,
            "trigger_on_remaining_7d": False, "target_group_ids": [1, 2], "supplier_note": "",
        }, {
            "group_id": 2, "enabled": False, "target_usable_count": 1, "min_usable_count": 1,
            "min_remaining_7d_percent": 25, "trigger_on_usable": True,
            "trigger_on_remaining_7d": False, "target_group_ids": [2], "supplier_note": "",
        }]
        manager.save_settings(settings)
        accounts = []
        patched = []

        def upstream(method, path, body=None):
            if path == "/api/admin/account-groups":
                return {"groups": [{"id": 1, "name": "Plus"}, {"id": 2, "name": "分流"}]}
            if path == "/api/admin/accounts" and method == "GET":
                return {"accounts": [dict(item) for item in accounts]}
            if path == "/api/admin/accounts" and method == "POST":
                accounts.append({"id": 10, "name": body["name"], "email": "live@example.invalid", "status": "active", "group_ids": []})
                return {"success": 1}
            if method == "PATCH":
                patched.extend(body["group_ids"])
                accounts[0]["group_ids"] = list(body["group_ids"])
                return {"message": "ok"}
            raise AssertionError((method, path))

        manager.upstream = upstream
        result = manager.supply(supplier["id"], 1, "rt_live", "")
        self.assertEqual(1, result["accepted"])
        self.assertEqual([1, 2], patched)
        history = manager.supply_history()
        self.assertEqual("accepted", history[0]["status"])
        self.assertEqual([1, 2], history[0]["target_group_ids"])

    def test_invalid_account_is_rejected_and_deleted(self):
        manager = self.make_manager()
        supplier = manager.create_supplier("供应商 C")
        settings = default_settings()
        settings["global"]["supplier_auto_import"] = True
        settings["groups"] = [{
            "group_id": 1, "enabled": True, "target_usable_count": 1, "min_usable_count": 1,
            "min_remaining_7d_percent": 25, "trigger_on_usable": True,
            "trigger_on_remaining_7d": False, "target_group_ids": [1], "supplier_note": "",
        }]
        manager.save_settings(settings)
        accounts = []
        deleted = []

        def upstream(method, path, body=None):
            if path == "/api/admin/account-groups":
                return {"groups": [{"id": 1, "name": "Plus"}]}
            if path == "/api/admin/accounts" and method == "GET":
                return {"accounts": [dict(item) for item in accounts]}
            if path == "/api/admin/accounts" and method == "POST":
                accounts.append({"id": 20, "name": body["name"], "status": "error", "group_ids": []})
                return {"success": 1}
            if method == "DELETE":
                deleted.append(int(path.rsplit("/", 1)[1]))
                return {"message": "deleted"}
            raise AssertionError((method, path))

        manager.upstream = upstream
        result = manager.supply(supplier["id"], 1, "expired-token", "")
        self.assertEqual(0, result["accepted"])
        self.assertEqual(1, result["failed"])
        self.assertEqual([20], deleted)
        self.assertEqual("rejected", manager.supply_history()[0]["status"])


if __name__ == "__main__":
    unittest.main()
