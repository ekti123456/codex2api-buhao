import tempfile
import unittest
from pathlib import Path

from server import Config, Manager, default_settings, is_usable, reconcile_policies, validate_settings


class ManagerLogicTests(unittest.TestCase):
    def make_manager(self) -> Manager:
        temp = Path(tempfile.mkdtemp())
        config = Config(
            host="127.0.0.1", port=0, base_url="https://example.invalid", admin_key="secret",
            admin_password="admin-password", supplier_password="supplier-password",
            settings_file=temp / "settings.json", audit_file=temp / "audit.jsonl",
            secure_cookie=False, session_seconds=3600, http_timeout=1,
        )
        return Manager(config)

    def test_usable_account_rules(self):
        self.assertTrue(is_usable({"status": "active", "health_tier": "healthy"}))
        self.assertTrue(is_usable({"status": "ready"}))
        self.assertFalse(is_usable({"status": "cooldown"}))
        self.assertFalse(is_usable({"status": "active", "enabled": False}))
        self.assertFalse(is_usable({"status": "active", "health_tier": "banned"}))

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
            "trigger_on_remaining_7d": False, "supplier_note": "",
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
            "trigger_on_remaining_7d": True, "supplier_note": "",
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
            "trigger_on_remaining_7d": True, "supplier_note": "",
        }]
        with self.assertRaises(ValueError):
            validate_settings(settings)


if __name__ == "__main__":
    unittest.main()
