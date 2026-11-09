from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from lib.newapi import NewApiClient  # noqa: E402
from reconcile import check_order, check_rate, match_chain  # noqa: E402


class ReconcileCoreTests(unittest.TestCase):
    def test_chain_match_requires_hash_recipient_and_exact_amount(self) -> None:
        expected = Decimal("5.000001")
        base = {
            "tx_hash": "0xabc",
            "to": "0xreceiver",
            "from": "0xpayer",
            "amount": expected,
            "confirmed": True,
        }
        wrong_recipient = {**base, "to": "0xother"}
        wrong_amount = {**base, "amount": Decimal("5.000002")}
        self.assertEqual(match_chain([wrong_recipient, wrong_amount, base], "0xabc", "0xreceiver", expected), [base])
        self.assertEqual(match_chain([wrong_recipient, wrong_amount], "0xabc", "0xreceiver", expected), [])

    def test_new_api_pending_only_is_not_a_reconciliation_difference(self) -> None:
        cfg = SimpleNamespace(rate_max_age_seconds=300, quota_per_usd=500000)
        row = check_order(
            "ORDER-PENDING",
            {"status": "pending", "user_id": 1},
            None,
            None,
            {},
            {},
            cfg,
            False,
            0,
            0,
            {"complete": True},
        )
        self.assertEqual(row["findings"], [])

    def test_rate_snapshot_recomputes_quota(self) -> None:
        now = datetime.now(timezone.utc)
        order = {
            "usd_per_token": Decimal("0.9995"),
            "token_per_cny": Decimal("0.14"),
            "usd_per_cny": Decimal("0.13993007"),
            "rate_observed_at": now,
            "confirmed_at": now + timedelta(seconds=5),
            "settled_usd": Decimal("4.9975"),
            "credit_quota": 2498750,
        }
        snapshot = {
            "usd_per_token": order["usd_per_token"],
            "token_per_cny": order["token_per_cny"],
            "usd_per_cny": order["usd_per_cny"],
            "source": "https://rates.example/cny.json",
        }
        order["rate_source"] = snapshot["source"]
        findings: list[str] = []
        check_rate(order, snapshot, SimpleNamespace(rate_max_age_seconds=300, quota_per_usd=500000), findings)
        self.assertEqual(findings, [])
        order["credit_quota"] = 1
        findings = []
        check_rate(order, snapshot, SimpleNamespace(rate_max_age_seconds=300, quota_per_usd=500000), findings)
        self.assertTrue(any("重算" in item for item in findings))

    def test_epay_pay_address_leaves_submit_php_to_official_client(self) -> None:
        from bootstrap import build_desired_options

        cfg = {
            "smtp": SimpleNamespace(host=""),
            "site": SimpleNamespace(system_name="Test", logo_text="Test", register_enabled=True, password_register_enabled=True, email_verification_enabled=True),
            "pay": SimpleNamespace(site_domain="example.com", epay_id="1000", epay_key="secret", price_cny=7.3, min_topup=1, callback_address=""),
        }
        options = build_desired_options(cfg)
        self.assertEqual(options["PayAddress"], "https://example.com/crypto-pay")
        self.assertFalse(options["PayAddress"].endswith("submit.php"))
        self.assertEqual(options["general_setting.quota_display_type"], "CUSTOM")
        self.assertEqual(options["general_setting.custom_currency_symbol"], "USDT")
        self.assertEqual(options["general_setting.custom_currency_exchange_rate"], "1.0")

    def test_bearer_token_takes_precedence_over_optional_basic_auth(self) -> None:
        client = NewApiClient("http://localhost", basic_auth_username="admin", basic_auth_password="secret")
        self.assertTrue(client._auth_header().startswith("Basic "))
        client.access_token = "new-api-session"
        self.assertEqual(client._auth_header(), "Bearer new-api-session")


if __name__ == "__main__":
    unittest.main()
