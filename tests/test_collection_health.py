"""
Tests for Collection Health: visit-counting, voucher classification, the
report builder, the global settings (credit period / visit method /
last-chance visit color), and the web routes.

Pure logic: coll_data (count_remaining_visits / classify_voucher_health /
build_collection_health / validate_collection_health_settings).
Store: the settings table round-trip.
Web: /manage/credit-period and /reports/collection-health.

Run:  python -m unittest tests.test_collection_health -v
"""

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import coll_store
from coll_data import (
    build_collection_health,
    classify_voucher_health,
    count_remaining_visits,
    validate_collection_health_settings,
)
from tests.test_coll_api import ApiTestCase


class CountRemainingVisitsTests(unittest.TestCase):
    today = date(2026, 10, 7)  # a Wednesday

    def test_weekly_accurate_counts_three_visits(self):
        # Mondays on/after today, up to and including the deadline: 12, 19, 26 Oct.
        self.assertEqual(
            count_remaining_visits("weekly", "mon", "", None, None, self.today, date(2026, 10, 26)),
            3)

    def test_weekly_accurate_deadline_before_first_visit_is_zero(self):
        self.assertEqual(
            count_remaining_visits("weekly", "mon", "", None, None, self.today, date(2026, 10, 10)),
            0)

    def test_weekly_accurate_stale_anchor_catches_up_without_counting_past_visits(self):
        # Same as no-anchor case: a Monday anchor from over a week ago still
        # lands on the same three future Mondays, not double-counted.
        stale_anchor = date(2026, 9, 28)
        self.assertEqual(
            count_remaining_visits("weekly", "mon", "", "", stale_anchor, self.today,
                                   date(2026, 10, 26)),
            3)

    def test_weekly_accurate_unschedulable_without_a_day_is_none(self):
        self.assertIsNone(
            count_remaining_visits("weekly", "", "", None, None, self.today, date(2026, 10, 26)))

    def test_biweekly_accurate_counts_two_visits(self):
        # Sept 1, 2026 is a Tuesday.
        self.assertEqual(
            count_remaining_visits("biweekly", "tue", "", "", date(2026, 9, 1), self.today,
                                   date(2026, 10, 27)),
            2)

    def test_monthly_accurate_counts_three_visits(self):
        # 2nd Tuesday of each month, no history: Oct 13, then Nov 10, then
        # Dec 8 -- all on/before the Dec 8 deadline (inclusive).
        self.assertEqual(
            count_remaining_visits("monthly", "tue", "2", None, None, self.today,
                                   date(2026, 12, 8)),
            3)

    def test_approximate_weekly_flat_interval(self):
        # 19 days left / 7-day interval = 2 (vs. accurate's 3 for the same window).
        self.assertEqual(
            count_remaining_visits("weekly", "mon", "", None, None, self.today, date(2026, 10, 26),
                                   method="approximate"),
            2)

    def test_approximate_biweekly_flat_interval(self):
        self.assertEqual(
            count_remaining_visits("biweekly", "", "", None, None, self.today, date(2026, 10, 27),
                                   method="approximate"),
            1)

    def test_approximate_monthly_flat_interval(self):
        self.assertEqual(
            count_remaining_visits("monthly", "", "", None, None, self.today, date(2026, 12, 7),
                                   method="approximate"),
            2)

    def test_approximate_ignores_cadence_day_and_anchor(self):
        with_anchor = count_remaining_visits("weekly", "", "", "", date(2026, 1, 1), self.today,
                                             date(2026, 10, 26), method="approximate")
        without_anchor = count_remaining_visits("weekly", "fri", "", None, None, self.today,
                                                date(2026, 10, 26), method="approximate")
        self.assertEqual(with_anchor, without_anchor)

    def test_approximate_unknown_cadence_is_none(self):
        self.assertIsNone(
            count_remaining_visits("daily", "", "", None, None, self.today, date(2026, 10, 26),
                                   method="approximate"))

    def test_approximate_deadline_before_today_clamps_to_zero(self):
        self.assertEqual(
            count_remaining_visits("weekly", "", "", None, None, self.today, date(2026, 10, 1),
                                   method="approximate"),
            0)


class ClassifyVoucherHealthTests(unittest.TestCase):
    today = date(2026, 10, 7)

    def test_age_over_credit_period_is_red(self):
        self.assertEqual(
            classify_voucher_health(date(2025, 1, 1), 30, "weekly", "mon", "", None, None,
                                    self.today),
            "red")

    def test_visits_over_one_is_green(self):
        # credit_period=12 -> deadline Oct 19; accurate weekly/mon from today gives 2 visits.
        self.assertEqual(
            classify_voucher_health(self.today, 12, "weekly", "mon", "", None, None, self.today),
            "green")

    def test_visits_equal_one_is_yellow(self):
        # credit_period=7 -> deadline Oct 14; only the Oct 12 visit falls inside.
        self.assertEqual(
            classify_voucher_health(self.today, 7, "weekly", "mon", "", None, None, self.today),
            "yellow")

    def test_visits_equal_zero_honors_last_visit_color(self):
        # credit_period=3 -> deadline Oct 10, before the first due date Oct 12.
        self.assertEqual(
            classify_voucher_health(self.today, 3, "weekly", "mon", "", None, None, self.today,
                                    last_visit_color="red"),
            "red")
        self.assertEqual(
            classify_voucher_health(self.today, 3, "weekly", "mon", "", None, None, self.today,
                                    last_visit_color="yellow"),
            "yellow")

    def test_unschedulable_beat_is_unknown(self):
        self.assertEqual(
            classify_voucher_health(self.today, 30, "weekly", "", "", None, None, self.today),
            "unknown")

    def test_visit_method_changes_the_bucket_for_the_same_voucher(self):
        # credit_period=12 -> deadline Oct 19. Accurate: 2 visits (green).
        # Approximate: 12 days / 7-day interval = 1 visit (yellow).
        self.assertEqual(
            classify_voucher_health(self.today, 12, "weekly", "mon", "", None, None, self.today,
                                    visit_method="accurate"),
            "green")
        self.assertEqual(
            classify_voucher_health(self.today, 12, "weekly", "mon", "", None, None, self.today,
                                    visit_method="approximate"),
            "yellow")

    def test_last_visit_color_red_shifts_the_whole_boundary(self):
        # credit_period=12 -> deadline Oct 19; accurate weekly/mon from today
        # gives exactly 2 visits (Oct 12, Oct 19).
        self.assertEqual(
            classify_voucher_health(self.today, 12, "weekly", "mon", "", None, None, self.today,
                                    last_visit_color="red"),
            "yellow")  # 2 visits is the shifted "one before last chance" slot
        # credit_period=7 -> deadline Oct 14; exactly 1 visit (Oct 12) -- the
        # last-chance visit itself, now red instead of yellow.
        self.assertEqual(
            classify_voucher_health(self.today, 7, "weekly", "mon", "", None, None, self.today,
                                    last_visit_color="red"),
            "red")
        # credit_period=3 -> deadline Oct 10, zero visits -- still red.
        self.assertEqual(
            classify_voucher_health(self.today, 3, "weekly", "mon", "", None, None, self.today,
                                    last_visit_color="red"),
            "red")

    def test_last_visit_color_red_needs_three_visits_for_green(self):
        # credit_period=19 -> deadline Oct 26; accurate weekly/mon from today
        # gives exactly 3 visits (Oct 12, 19, 26) -> green even under "red".
        self.assertEqual(
            classify_voucher_health(self.today, 19, "weekly", "mon", "", None, None, self.today,
                                    last_visit_color="red"),
            "green")


class BuildCollectionHealthTests(unittest.TestCase):
    today = date(2026, 10, 7)
    settings = {"credit_period_days": 12, "visit_method": "accurate", "last_visit_color": "yellow"}

    def _voucher(self, bill_no, date_str, balance):
        return {"bill_no": bill_no, "date": date_str, "amount": balance, "paid": "0",
                "balance": balance, "beat": "North", "salesman": "sm1"}

    def test_buckets_vouchers_by_classification(self):
        beat_row = {"name": "North", "cadence": "weekly", "cadence_day": "mon", "cadence_start": ""}
        vouchers = [
            self._voucher("G1", "2026-10-07", "50.00"),   # deadline Oct19 -> 2 visits -> green
            self._voucher("Y1", "2026-10-02", "30.00"),   # deadline Oct14 -> 1 visit -> yellow
            self._voucher("R1", "2025-01-01", "20.00"),   # way overdue -> red
        ]
        result = build_collection_health(beat_row, vouchers, self.settings, [], [], self.today)
        self.assertEqual([v["bill_no"] for v in result["buckets"]["green"]], ["G1"])
        self.assertEqual([v["bill_no"] for v in result["buckets"]["yellow"]], ["Y1"])
        self.assertEqual([v["bill_no"] for v in result["buckets"]["red"]], ["R1"])
        self.assertEqual(result["buckets"]["unknown"], [])
        self.assertEqual(result["counts"], {"green": 1, "yellow": 1, "red": 1, "unknown": 0})
        self.assertEqual(result["totals"]["green"], 50)
        self.assertIsNone(result["anchor"])

    def test_unschedulable_beat_puts_non_overdue_vouchers_in_unknown(self):
        beat_row = {"name": "North", "cadence": "weekly", "cadence_day": "", "cadence_start": ""}
        vouchers = [self._voucher("U1", "2026-10-07", "40.00")]
        result = build_collection_health(beat_row, vouchers, self.settings, [], [], self.today)
        self.assertEqual([v["bill_no"] for v in result["buckets"]["unknown"]], ["U1"])

    def test_anchor_prefers_staged_over_archived(self):
        beat_row = {"name": "North", "cadence": "weekly", "cadence_day": "mon", "cadence_start": ""}
        staged = [(Path("coll-staged.json"), {"selection": ["North", "sm1"], "date": "2026-09-28"})]
        archived = [(Path("coll-archived.json"), {"selection": ["North", "sm1"], "date": "2026-08-01"})]
        result = build_collection_health(beat_row, [], self.settings, staged, archived, self.today)
        self.assertEqual(result["anchor"], date(2026, 9, 28))


class ValidateCollectionHealthSettingsTests(unittest.TestCase):
    def test_valid_input_has_no_errors(self):
        errors, days = validate_collection_health_settings("30", "accurate", "yellow")
        self.assertEqual(errors, [])
        self.assertEqual(days, 30)

    def test_non_numeric_credit_period_is_rejected(self):
        errors, days = validate_collection_health_settings("abc", "accurate", "yellow")
        self.assertTrue(errors)
        self.assertIsNone(days)

    def test_non_positive_credit_period_is_rejected(self):
        errors, days = validate_collection_health_settings("0", "accurate", "yellow")
        self.assertTrue(errors)
        self.assertIsNone(days)

    def test_invalid_visit_method_is_rejected(self):
        errors, _ = validate_collection_health_settings("30", "bogus", "yellow")
        self.assertTrue(errors)

    def test_invalid_last_visit_color_is_rejected(self):
        errors, _ = validate_collection_health_settings("30", "accurate", "bogus")
        self.assertTrue(errors)


class CollectionHealthSettingsStoreTests(unittest.TestCase):
    """Real SQLite DB in a temp dir (same pattern as BeatCadenceStoreTests)."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        (self.tmp / "data").mkdir()
        self._patch = patch.object(coll_store, "DATA_DIR", self.tmp / "data")
        self._patch.start()
        coll_store.init_db()

    def tearDown(self):
        self._patch.stop()
        self._tmpdir.cleanup()

    def test_default_seed_values(self):
        settings = coll_store.get_collection_health_settings()
        self.assertEqual(settings, {"credit_period_days": 30, "visit_method": "accurate",
                                    "last_visit_color": "yellow"})

    def test_update_round_trips(self):
        coll_store.update_collection_health_settings(45, "approximate", "red")
        self.assertEqual(coll_store.get_collection_health_settings(),
                         {"credit_period_days": 45, "visit_method": "approximate",
                          "last_visit_color": "red"})


class CollectionHealthWebTests(ApiTestCase):
    """Routes against the live app: the settings form and the report page."""

    def setUp(self):
        super().setUp()
        self._add_user("dist1", "distributor", "pw-dist")
        self._add_user("sm1", "salesman", "pw-sm1")
        self._add_user("sm2", "salesman", "pw-sm2")
        self._add_beat("North", "sm1")
        self._add_beat("South", "sm2")
        self.dist = self._login("dist1", "pw-dist")

    def _add_voucher_dated(self, bill_no, beat, salesman, date_str, balance="100.00"):
        conn = coll_store.get_db()
        try:
            conn.execute(
                "INSERT INTO vouchers "
                "(bill_no, date, amount, balance, beat, salesman, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (bill_no, date_str, balance, balance, beat, salesman, "test", "2026-01-01T00:00:00"))
            conn.commit()
        finally:
            conn.close()

    def test_settings_form_shows_defaults(self):
        status, body = self._get(self.dist, "/manage/credit-period")
        self.assertEqual(status, 200)
        self.assertIn("30", body)

    def test_settings_update_round_trips(self):
        status, _ = self._post(self.dist, "/manage/credit-period", {
            "credit_period_days": "45", "visit_method": "approximate", "last_visit_color": "red"})
        self.assertEqual(status, 200)
        self.assertEqual(coll_store.get_collection_health_settings(),
                         {"credit_period_days": 45, "visit_method": "approximate",
                          "last_visit_color": "red"})

    def test_settings_invalid_value_is_not_saved(self):
        status, body = self._post(self.dist, "/manage/credit-period", {
            "credit_period_days": "not-a-number", "visit_method": "accurate",
            "last_visit_color": "yellow"})
        self.assertEqual(status, 200)
        self.assertIn("Invalid credit period", body)
        self.assertEqual(coll_store.get_collection_health_settings()["credit_period_days"], 30)

    def test_settings_form_rejected_for_non_distributor(self):
        sm1 = self._login("sm1", "pw-sm1")
        status, body = self._get(sm1, "/manage/credit-period")
        self.assertEqual(status, 200)
        self.assertIn("permission for this action", body)

    def test_report_lists_only_own_beats_for_salesman(self):
        sm1 = self._login("sm1", "pw-sm1")
        status, body = self._get(sm1, "/reports/collection-health")
        self.assertEqual(status, 200)
        self.assertIn("North", body)
        self.assertNotIn("South", body)

    def test_report_buckets_and_drilldown(self):
        # Credit period of a full year leaves dozens of weekly visits before
        # the deadline regardless of which weekday "today" happens to be when
        # this test runs — green is the only possible outcome, so the test
        # doesn't need to pin a fixed "today" the way the pure unit tests do.
        conn = coll_store.get_db()
        try:
            with conn:
                conn.execute(
                    "UPDATE beats SET cadence='weekly', cadence_day='mon' WHERE name='North'")
        finally:
            conn.close()
        self._post(self.dist, "/manage/credit-period", {
            "credit_period_days": "365", "visit_method": "accurate", "last_visit_color": "yellow"})
        self._add_voucher_dated("G1", "North", "sm1", date.today().isoformat(), "50.00")

        status, body = self._get(self.dist, "/reports/collection-health?beat=North")
        self.assertEqual(status, 200)
        self.assertIn("Green (1)", body)

        status, body = self._get(self.dist, "/reports/collection-health?beat=North&bucket=green")
        self.assertEqual(status, 200)
        self.assertIn("G1", body)


if __name__ == "__main__":
    unittest.main()
