"""
Tests for beat cadence, due dates and the collection pipeline grouping.

Pure logic: coll_data (cadence / due list) and coll_orchestrate (pipeline).
Store: beats cadence columns, the v1 rebuild path, and the list loaders.

Run:  python -m unittest tests.test_cadence_pipeline -v
"""

import json
import sys
import unittest
import urllib.error
import urllib.request
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import coll_data
import coll_orchestrate
import coll_store
from coll_data import (
    CADENCE_REGISTRY, CadenceSpec, build_due_list, next_due_date, nth_weekday_of_month,
    report_date, validate_cadence,
)
from coll_orchestrate import (
    build_collection_pipeline, build_onboarding_pipeline, build_setup_gaps, collection_stage,
    find_and_flag_salesman_mismatches,
)
from tests.test_coll_api import ApiTestCase


MON = 0  # weekday index used in assertions below (Monday)


class ValidateCadenceTests(unittest.TestCase):
    def test_valid_weekly_needs_day(self):
        self.assertIsNone(validate_cadence("weekly", "mon", "", ""))
        self.assertIsNotNone(validate_cadence("weekly", "", "", ""))
        self.assertIsNotNone(validate_cadence("weekly", "funday", "", ""))

    def test_unknown_cadence_rejected(self):
        self.assertIsNotNone(validate_cadence("daily", "", "", ""))

    def test_biweekly_needs_day_and_accepts_blank_start(self):
        self.assertIsNotNone(validate_cadence("biweekly", "", "", ""))
        self.assertIsNone(validate_cadence("biweekly", "fri", "", ""))

    def test_monthly_needs_day_and_week(self):
        self.assertIsNone(validate_cadence("monthly", "mon", "1", ""))
        self.assertIsNotNone(validate_cadence("monthly", "", "1", ""))
        self.assertIsNotNone(validate_cadence("monthly", "mon", "", ""))
        self.assertIsNotNone(validate_cadence("monthly", "mon", "5", ""))

    def test_start_date_must_parse(self):
        # Oct 5, 2026 is a Monday.
        self.assertIsNone(validate_cadence("biweekly", "mon", "", "2026-10-05"))
        self.assertIsNotNone(validate_cadence("biweekly", "mon", "", "05/10/2026"))

    def test_start_date_must_match_the_collection_day_for_weekly_and_biweekly(self):
        # Oct 5, 2026 is a Monday, not a Friday.
        self.assertIsNotNone(validate_cadence("weekly", "fri", "", "2026-10-05"))
        self.assertIsNotNone(validate_cadence("biweekly", "fri", "", "2026-10-05"))
        self.assertIsNone(validate_cadence("weekly", "mon", "", "2026-10-05"))
        self.assertIsNone(validate_cadence("biweekly", "mon", "", "2026-10-05"))

    def test_start_date_must_match_the_week_and_day_for_monthly(self):
        # Oct 13, 2026 is the 2nd Tuesday of October; Oct 6 is the 1st.
        self.assertIsNone(validate_cadence("monthly", "tue", "2", "2026-10-13"))
        self.assertIsNotNone(validate_cadence("monthly", "tue", "2", "2026-10-06"))
        self.assertIsNotNone(validate_cadence("monthly", "tue", "2", "2026-10-05"))  # wrong weekday too


class NextDueDateTests(unittest.TestCase):
    today = date(2026, 10, 7)  # a Wednesday

    def test_weekly_no_history_is_next_chosen_weekday_from_today(self):
        self.assertEqual(next_due_date("weekly", "fri", "", None, None, self.today),
                         date(2026, 10, 9))

    def test_weekly_no_history_on_the_chosen_day_is_today(self):
        self.assertEqual(next_due_date("weekly", "wed", "", None, None, self.today), self.today)

    def test_weekly_with_anchor_is_next_chosen_day_after_seven_days(self):
        anchor = date(2026, 9, 28)  # a Monday; +7 = 2026-10-05 (Monday)
        self.assertEqual(next_due_date("weekly", "mon", "", None, anchor, self.today),
                         date(2026, 10, 5))
        self.assertEqual(next_due_date("weekly", "thu", "", None, anchor, self.today),
                         date(2026, 10, 8))

    def test_weekly_without_day_cannot_be_scheduled(self):
        self.assertIsNone(next_due_date("weekly", "", "", None, None, self.today))

    def test_biweekly_with_anchor_adds_14_days(self):
        # Sept 1, 2026 is a Tuesday; +14 days lands on another Tuesday.
        self.assertEqual(next_due_date("biweekly", "tue", "", None, date(2026, 9, 1), self.today),
                         date(2026, 9, 15))

    def test_biweekly_no_history_uses_start_date(self):
        start = date(2026, 10, 20)  # a Tuesday
        self.assertEqual(next_due_date("biweekly", "tue", "", start, None, self.today), start)

    def test_biweekly_no_history_no_start_uses_next_matching_day(self):
        # Same fallback shape as weekly: next Friday on/after today (Oct 7, a Wednesday).
        self.assertEqual(next_due_date("biweekly", "fri", "", None, None, self.today),
                         date(2026, 10, 9))

    def test_biweekly_without_day_cannot_be_scheduled(self):
        self.assertIsNone(next_due_date("biweekly", "", "", None, None, self.today))

    def test_monthly_with_anchor_is_that_week_day_in_the_following_month(self):
        # Anchor in September -> due in October (the month after); 2nd
        # Tuesday of October 2026 is Oct 13.
        self.assertEqual(
            next_due_date("monthly", "tue", "2", "", date(2026, 9, 15), self.today),
            date(2026, 10, 13))

    def test_monthly_no_history_uses_this_months_slot_if_still_ahead(self):
        # 2nd Tuesday of October 2026 is Oct 13, still ahead of today (Oct 7).
        self.assertEqual(next_due_date("monthly", "tue", "2", None, None, self.today),
                         date(2026, 10, 13))

    def test_monthly_no_history_rolls_to_next_month_if_this_months_slot_passed(self):
        # 1st Monday of October 2026 is Oct 5, already before today (Oct 7) ->
        # rolls to the 1st Monday of November, Nov 2.
        self.assertEqual(next_due_date("monthly", "mon", "1", None, None, self.today),
                         date(2026, 11, 2))

    def test_monthly_missing_day_or_week_cannot_be_scheduled(self):
        self.assertIsNone(next_due_date("monthly", "", "1", "", None, self.today))
        self.assertIsNone(next_due_date("monthly", "mon", "", "", None, self.today))

    def test_unknown_cadence_cannot_be_scheduled(self):
        self.assertIsNone(next_due_date("", "", "", "", date(2026, 9, 1), self.today))

    def test_registering_a_new_cadence_needs_no_other_code_change(self):
        """Proof that adding a cadence is registry-only: a throwaway daily
        cadence, registered directly into CADENCE_REGISTRY, works through
        next_due_date and validate_cadence with no dispatch code touched."""
        CADENCE_REGISTRY["daily"] = CadenceSpec(
            frozenset(), lambda day, week, start, anchor, today: today + timedelta(days=1), 1, None)
        try:
            self.assertIsNone(validate_cadence("daily", "", "", ""))
            self.assertEqual(next_due_date("daily", "", "", "", None, self.today),
                             self.today + timedelta(days=1))
        finally:
            del CADENCE_REGISTRY["daily"]


class NthWeekdayOfMonthTests(unittest.TestCase):
    def test_first_occurrence(self):
        # October 2026 starts on a Thursday.
        self.assertEqual(nth_weekday_of_month(2026, 10, 0, 1), date(2026, 10, 5))  # 1st Mon

    def test_fourth_occurrence_always_exists(self):
        self.assertEqual(nth_weekday_of_month(2026, 10, 0, 4), date(2026, 10, 26))  # 4th Mon

    def test_matches_known_second_tuesday(self):
        self.assertEqual(nth_weekday_of_month(2026, 11, 1, 2), date(2026, 11, 10))


class ReportDateTests(unittest.TestCase):
    def test_uses_date_field_when_present(self):
        p = Path("coll20260101-beat-x.json")
        self.assertEqual(report_date(p, {"date": "2026-10-02"}), date(2026, 10, 2))

    def test_falls_back_to_filename_stamp(self):
        p = Path("coll20260628-beat_salesman-cp1beat1_cp1salesman1.json")
        self.assertEqual(report_date(p, {}), date(2026, 6, 28))

    def test_none_when_neither_is_usable(self):
        self.assertIsNone(report_date(Path("other.json"), {}))


def _list(path_stem, beat, stages, vouchers=None, date_str=None):
    data = {"selection_type": "beat", "selection": [beat], "stages": stages,
            "vouchers": vouchers or []}
    if date_str:
        data["date"] = date_str
    return (Path(path_stem + ".json"), data)


class BuildDueListTests(unittest.TestCase):
    today = date(2026, 10, 7)

    def _beat(self, name, cadence="weekly", day="mon", week="", start="", salesman="s1"):
        return {"name": name, "salesman": salesman, "cadence": cadence,
                "cadence_day": day, "cadence_week": week, "cadence_start": start}

    def test_buckets(self):
        beats = [
            self._beat("late", day="mon"),         # anchor 2026-09-21 -> due 09-28 -> overdue
            self._beat("today", day="wed"),        # no history -> due today
            self._beat("soon", day="fri"),         # no history -> due 10-09 (in 2 days)
            self._beat("far", day="tue"),          # no history -> due 10-13, outside window
            self._beat("unset", day=""),           # weekly without day -> needs setup
        ]
        posted = [_list("archive/coll20260921-beat-late", "late", {"post": "confirmed"})]
        result = build_due_list(beats, [], posted, self.today, window_days=3)
        self.assertEqual([r["beat"] for r in result["overdue"]], ["late"])
        self.assertEqual([r["beat"] for r in result["due_today"]], ["today"])
        self.assertEqual([r["beat"] for r in result["upcoming"]], ["soon"])
        self.assertEqual([r["beat"] for r in result["needs_setup"]], ["unset"])
        self.assertNotIn("far", [r["beat"] for r in sum(result.values(), [])])

    def test_active_list_is_the_anchor_over_last_posted(self):
        beats = [self._beat("b1", day="mon")]
        staged = [_list("staging/coll20261005-beat-b1", "b1", {"start": "confirmed"})]
        posted = [_list("archive/coll20260921-beat-b1", "b1", {"post": "confirmed"})]
        result = build_due_list(beats, staged, posted, self.today, window_days=14)
        row = (result["upcoming"] + result["due_today"] + result["overdue"])[0]
        self.assertEqual(row["anchor"], date(2026, 10, 5))
        self.assertTrue(row["in_progress"])
        self.assertEqual(row["next_due"], date(2026, 10, 12))

    def test_window_bounds_are_inclusive(self):
        beats = [self._beat("edge", day="fri")]  # no history: due 10-09 = today + 2
        result = build_due_list(beats, [], [], self.today, window_days=2)
        self.assertEqual([r["beat"] for r in result["upcoming"]], ["edge"])
        result = build_due_list(beats, [], [], self.today, window_days=1)
        self.assertEqual(result["upcoming"], [])

    def test_monthly_beat_with_day_and_week_when_no_history(self):
        # 2nd Tuesday of October 2026 is Oct 13 (today is Oct 7) -> upcoming.
        beats = [self._beat("m1", cadence="monthly", day="tue", week="2")]
        result = build_due_list(beats, [], [], self.today, window_days=7)
        self.assertEqual([r["beat"] for r in result["upcoming"]], ["m1"])

    def test_monthly_without_day_or_week_needs_setup(self):
        beats = [self._beat("m1", cadence="monthly", day="", week="")]
        result = build_due_list(beats, [], [], self.today, window_days=3)
        self.assertEqual([r["beat"] for r in result["needs_setup"]], ["m1"])

    def test_biweekly_without_day_needs_setup(self):
        beats = [self._beat("f1", cadence="biweekly", day="", start="")]
        result = build_due_list(beats, [], [], self.today, window_days=3)
        self.assertEqual([r["beat"] for r in result["needs_setup"]], ["f1"])

    def test_biweekly_with_day_but_no_start_or_history_is_schedulable(self):
        # today is Oct 7, 2026 (Wednesday) -> next Friday on/after today = Oct 9.
        beats = [self._beat("f1", cadence="biweekly", day="fri", start="")]
        result = build_due_list(beats, [], [], self.today, window_days=3)
        self.assertEqual([r["beat"] for r in result["upcoming"]], ["f1"])


class CollectionStageTests(unittest.TestCase):
    def test_stage_mapping(self):
        """Stage keys/labels match the main menu: Approve Collection List,
        Submit Collections (not-started/in-progress/returned all land here),
        Approve Collections, Post Collections."""
        self.assertEqual(collection_stage({"start": "new"}), "waiting_start")
        self.assertEqual(collection_stage({"start": "confirmed", "submit": ""}), "submit")
        self.assertEqual(collection_stage({"start": "confirmed", "submit": "inprogress"}),
                         "submit")
        self.assertEqual(collection_stage({"start": "confirmed", "submit": "returned"}),
                         "submit")
        self.assertEqual(collection_stage({"start": "confirmed", "submit": "submitted"}),
                         "waiting_submit")
        self.assertEqual(collection_stage({"start": "confirmed", "submit": "confirmed"}),
                         "waiting_post")


class BuildCollectionPipelineTests(unittest.TestCase):
    today = date(2026, 10, 4)

    def _v(self, bill, salesman, balance, **extra):
        v = {"bill_no": bill, "salesman": salesman, "balance": balance}
        v.update(extra)
        return v

    def test_collection_lists_grouped_by_stage_with_age_and_totals(self):
        staged = [
            _list("staging/coll20261001-beat-a", "A", {"start": "new"},
                  [self._v("1", "s1", "100.00"), self._v("2", "s2", "50.50")]),
            _list("staging/coll20261003-beat-b", "B", {"start": "confirmed", "submit": "returned"},
                  [self._v("3", "s1", "10.00")]),
        ]
        pipe = build_collection_pipeline(staged, self.today)
        waiting = pipe["waiting_start"]
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0]["beat"], "A")
        self.assertEqual(waiting[0]["bills"], 2)
        self.assertEqual(waiting[0]["balance"], Decimal("150.50"))
        self.assertEqual(waiting[0]["salesmen"], ["s1", "s2"])
        self.assertEqual(waiting[0]["age_days"], 3)
        self.assertTrue(waiting[0]["stale"])
        returned = pipe["submit"]
        self.assertTrue(returned[0]["returned"])
        self.assertFalse(returned[0]["stale"])

    def test_salesman_scope_only_counts_own_vouchers(self):
        staged = [
            _list("staging/coll20261004-beat-a", "A", {"start": "new"},
                  [self._v("1", "s1", "100.00"), self._v("2", "s2", "50.00")]),
            _list("staging/coll20261004-beat-z", "Z", {"start": "new"},
                  [self._v("9", "s2", "5.00")]),
        ]
        pipe = build_collection_pipeline(staged, self.today, salesman="s1")
        items = pipe["waiting_start"]
        self.assertEqual([i["beat"] for i in items], ["A"])
        self.assertEqual(items[0]["bills"], 1)
        self.assertEqual(items[0]["balance"], Decimal("100.00"))


class BuildOnboardingPipelineTests(unittest.TestCase):
    today = date(2026, 10, 4)

    def test_onboarding_batch_split_by_voucher_stage(self):
        batch = {
            "created_by": "dist", "created_at": "2026-10-02T09:00:00",
            "flags": [{"id": 1, "bill_no": "2", "kind": "voucher_amount", "status": "open"}],
            "vouchers": [
                {"bill_no": "1", "salesman": "s1", "balance": "10.00", "review_status": "pending"},
                {"bill_no": "2", "salesman": "s1", "balance": "20.00", "review_status": "reviewed",
                 "approved_at": "2026-10-02T10:00:00"},
                {"bill_no": "3", "salesman": "s2", "balance": "30.00", "review_status": "reviewed"},
            ],
        }
        pipe = build_onboarding_pipeline([(Path("staging/addv1.json"), batch)], self.today)
        pending = pipe["pending_review"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["bills"], 1)
        self.assertEqual(pending[0]["balance"], Decimal("10.00"))
        self.assertEqual(pending[0]["age_days"], 2)
        self.assertEqual(len(pipe["awaiting_approval"]), 1)
        resolution = pipe["awaiting_resolution"]
        self.assertEqual(resolution[0]["open_flags"], 1)

    def test_onboarding_salesman_scope(self):
        batch = {"created_by": "dist", "created_at": "2026-10-02T09:00:00", "flags": [],
                 "vouchers": [
                     {"bill_no": "1", "salesman": "s1", "balance": "10.00", "review_status": "pending"},
                     {"bill_no": "2", "salesman": "s2", "balance": "20.00", "review_status": "pending"},
                 ]}
        pipe = build_onboarding_pipeline([(Path("staging/addv1.json"), batch)], self.today, salesman="s2")
        items = pipe["pending_review"]
        self.assertEqual(items[0]["bills"], 1)
        self.assertEqual(items[0]["salesmen"], ["s2"])


class BuildSetupGapsTests(unittest.TestCase):
    today = date(2026, 10, 7)

    def _beat(self, name, cadence="weekly", day="mon", week="", start="", salesman="s1"):
        return {"name": name, "salesman": salesman, "cadence": cadence,
                "cadence_day": day, "cadence_week": week, "cadence_start": start}

    def _settings(self, **over):
        base = {"credit_period_days": 30, "visit_method": "accurate", "last_visit_color": "yellow"}
        base.update(over)
        return base

    def _users(self, *roles):
        return [{"name": f"u{i}", "role": r} for i, r in enumerate(roles)]

    def test_no_beats_blocks_everything(self):
        gaps = build_setup_gaps([], self._users("distributor"), set(), [], [], [],
                                self._settings(), self.today)
        self.assertTrue(gaps["blocking"]["no_beats"])
        self.assertFalse(gaps["all_clear"])

    def test_missing_supervisor_and_salesman_users_flagged(self):
        beats = [self._beat("B1")]
        gaps = build_setup_gaps(beats, self._users("distributor"), {"B1"}, [], [], [],
                                self._settings(), self.today)
        self.assertTrue(gaps["blocking"]["no_supervisor"])
        self.assertTrue(gaps["blocking"]["no_salesman_user"])

    def test_beat_without_salesman_flagged(self):
        beats = [self._beat("B1", salesman="")]
        users = self._users("distributor", "supervisor", "salesman")
        gaps = build_setup_gaps(beats, users, {"B1"}, [], [], [], self._settings(), self.today)
        self.assertEqual(gaps["blocking"]["no_salesman_beats"], ["B1"])
        self.assertEqual(gaps["by_beat"][0]["badges"], ["no_salesman"])

    def test_beat_needing_cadence_reuses_due_list(self):
        beats = [self._beat("B1", day="")]  # weekly, no day -> needs_setup
        users = self._users("supervisor", "salesman")
        gaps = build_setup_gaps(beats, users, {"B1"}, [], [], [], self._settings(), self.today)
        self.assertEqual([r["beat"] for r in gaps["blocking"]["needs_cadence_beats"]], ["B1"])

    def test_beat_with_zero_vouchers_and_no_batch_is_missing(self):
        beats = [self._beat("B1")]
        users = self._users("supervisor", "salesman")
        gaps = build_setup_gaps(beats, users, set(), [], [], [], self._settings(), self.today)
        self.assertEqual(gaps["data_feed"]["no_voucher_beats"], ["B1"])

    def test_beat_with_master_data_vouchers_is_not_missing(self):
        beats = [self._beat("B1")]
        users = self._users("supervisor", "salesman")
        gaps = build_setup_gaps(beats, users, {"B1"}, [], [], [], self._settings(), self.today)
        self.assertEqual(gaps["data_feed"]["no_voucher_beats"], [])

    def test_beat_with_unposted_addv_voucher_is_in_progress_not_missing(self):
        beats = [self._beat("B1")]
        users = self._users("supervisor", "salesman")
        batch = {"created_by": "d1", "created_at": "2026-10-07T09:00:00", "flags": [],
                 "vouchers": [{"bill_no": "1", "beat": "B1", "salesman": "s1", "balance": "10.00",
                              "review_status": "pending"}]}
        gaps = build_setup_gaps(beats, users, set(), [(Path("staging/addv1.json"), batch)],
                                [], [], self._settings(), self.today)
        self.assertEqual(gaps["data_feed"]["no_voucher_beats"], [])

    def test_posted_addv_voucher_alone_does_not_cover_the_beat(self):
        """Once posted, a voucher should show up in vouchers_with_data — a
        posted-only batch (coverage not reflected there) must not mask the
        gap, since only vouchers_with_data stands for real master-data
        coverage."""
        beats = [self._beat("B1")]
        users = self._users("supervisor", "salesman")
        batch = {"created_by": "d1", "created_at": "2026-10-07T09:00:00", "flags": [],
                 "vouchers": [{"bill_no": "1", "beat": "B1", "salesman": "s1", "balance": "10.00",
                              "review_status": "reviewed", "approved_at": "2026-10-07T10:00:00",
                              "posted_at": "2026-10-07T11:00:00"}]}
        gaps = build_setup_gaps(beats, users, set(), [(Path("staging/addv1.json"), batch)],
                                [], [], self._settings(), self.today)
        self.assertEqual(gaps["data_feed"]["no_voucher_beats"], ["B1"])

    def test_stale_collection_and_onboarding_counts(self):
        beats = [self._beat("B1")]
        users = self._users("supervisor", "salesman")
        staged = [_list("staging/coll20261001-beat-B1", "B1", {"start": "new"},
                        [{"bill_no": "1", "salesman": "s1", "balance": "10.00"}])]
        batch = {"created_by": "d1", "created_at": "2026-10-01T09:00:00", "flags": [],
                 "vouchers": [{"bill_no": "2", "beat": "B1", "salesman": "s1", "balance": "20.00",
                              "review_status": "pending"}]}
        gaps = build_setup_gaps(beats, users, {"B1"}, [(Path("staging/addv1.json"), batch)],
                                staged, [], self._settings(), self.today)
        self.assertEqual(gaps["stale"]["collection_count"], 1)
        self.assertEqual(gaps["stale"]["onboarding_count"], 1)

    def test_config_at_default_flagged_and_cleared(self):
        beats = [self._beat("B1")]
        users = self._users("supervisor", "salesman")
        gaps = build_setup_gaps(beats, users, {"B1"}, [], [], [], self._settings(), self.today)
        self.assertTrue(gaps["config"]["at_default"])
        gaps2 = build_setup_gaps(beats, users, {"B1"}, [], [], [],
                                 self._settings(credit_period_days=45), self.today)
        self.assertFalse(gaps2["config"]["at_default"])

    def test_by_beat_sorts_worst_first(self):
        beats = [self._beat("B1", salesman=""), self._beat("B2", day=""),
                 self._beat("B3", salesman="", day="")]
        users = self._users("supervisor", "salesman")
        gaps = build_setup_gaps(beats, users, set(), [], [], [], self._settings(), self.today)
        self.assertEqual(gaps["by_beat"][0]["beat"], "B3")
        self.assertEqual(set(gaps["by_beat"][0]["badges"]),
                         {"no_salesman", "no_cadence", "no_vouchers"})

    def test_all_clear_when_fully_configured(self):
        beats = [self._beat("B1")]
        users = self._users("supervisor", "salesman")
        gaps = build_setup_gaps(beats, users, {"B1"}, [], [], [], self._settings(), self.today)
        self.assertTrue(gaps["all_clear"])


class BeatCadenceStoreTests(unittest.TestCase):
    """Real SQLite DB in a temp dir (same pattern as test_coll_store)."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        (self.tmp / "data").mkdir()
        (self.tmp / "staging").mkdir()
        (self.tmp / "archive").mkdir()
        self._patches = [
            patch.object(coll_store, "DATA_DIR", self.tmp / "data"),
            patch.object(coll_store, "STAGING_DIR", self.tmp / "staging"),
            patch.object(coll_store, "ARCHIVE_DIR", self.tmp / "archive"),
        ]
        for p in self._patches:
            p.start()
        coll_store.init_db()
        conn = coll_store.get_db()
        with conn:
            conn.execute("INSERT INTO users (name, role, password_hash) VALUES ('sm1', 'salesman', 'x')")
        conn.close()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()

    def _beat_row(self, name):
        return next(b for b in coll_store.load_beats_raw() if b["name"] == name)

    def test_create_and_update_cadence(self):
        coll_store.create_beat("B1", "sm1", "weekly", "fri", "", "")
        row = self._beat_row("B1")
        self.assertEqual((row["cadence"], row["cadence_day"], row["cadence_start"]),
                         ("weekly", "fri", ""))
        coll_store.update_beat_cadence("B1", "monthly", "mon", "2", "")
        row = self._beat_row("B1")
        self.assertEqual((row["cadence"], row["cadence_day"], row["cadence_week"]),
                         ("monthly", "mon", "2"))

    def test_existing_beats_default_to_weekly_without_day(self):
        conn = coll_store.get_db()
        with conn:
            conn.execute("INSERT INTO beats (name, salesman) VALUES ('old', 'sm1')")
        conn.close()
        row = self._beat_row("old")
        self.assertEqual((row["cadence"], row["cadence_day"]), ("weekly", ""))

    def test_update_unknown_beat_raises(self):
        with self.assertRaises(ValueError):
            coll_store.update_beat_cadence("nope", "weekly", "mon", "", "")

    def test_cadence_columns_survive_v1_rebuild(self):
        """An install still at schema version 0 rebuilds beats in _migrate_schema_v1,
        which copies only name/salesman. Cadence must be re-added afterwards."""
        coll_store.create_beat("B2", "sm1", "biweekly", "", "", "2026-10-20")
        conn = coll_store.get_db()
        with conn:
            conn.execute("ALTER TABLE beats DROP COLUMN cadence")
            conn.execute("ALTER TABLE beats DROP COLUMN cadence_day")
            conn.execute("ALTER TABLE beats DROP COLUMN cadence_week")
            conn.execute("ALTER TABLE beats DROP COLUMN cadence_start")
            conn.execute("PRAGMA user_version = 0")
        conn.close()
        coll_store.init_db()
        conn = coll_store.get_db()
        try:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(beats)").fetchall()]
        finally:
            conn.close()
        self.assertIn("cadence", cols)
        self.assertIn("cadence_week", cols)
        self.assertIn("cadence_start", cols)
        row = self._beat_row("B2")
        self.assertEqual(row["cadence"], "weekly")  # rebuilt rows take the default

    def test_fortnightly_rows_are_renamed_to_biweekly(self):
        """Production beats stored under the old 'fortnightly' value must come
        back as 'biweekly' after init_db() — same behavior, clearer word."""
        conn = coll_store.get_db()
        with conn:
            conn.execute(
                "INSERT INTO beats (name, salesman, cadence, cadence_start)"
                " VALUES ('old-fn', 'sm1', 'fortnightly', '2026-10-20')")
        conn.close()
        coll_store.init_db()
        row = self._beat_row("old-fn")
        self.assertEqual(row["cadence"], "biweekly")
        self.assertEqual(row["cadence_start"], "2026-10-20")  # untouched

    def test_cadence_week_defaults_blank_for_pre_existing_monthly_beats(self):
        """A monthly beat created before cadence_week existed (old fixed-date
        behavior) is left blank, not guessed -- it falls into Needs Setup
        until the distributor re-picks a weekday + week."""
        conn = coll_store.get_db()
        with conn:
            conn.execute(
                "INSERT INTO beats (name, salesman, cadence, cadence_start)"
                " VALUES ('old-monthly', 'sm1', 'monthly', '2026-10-20')")
        conn.close()
        coll_store.init_db()
        row = self._beat_row("old-monthly")
        self.assertEqual(row["cadence_week"], "")


class StagedReportLoaderTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        (self.tmp / "staging").mkdir()
        self._patch = patch.object(coll_store, "STAGING_DIR", self.tmp / "staging")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmpdir.cleanup()

    def test_sidecar_installment_files_are_skipped(self):
        s = self.tmp / "staging"
        (s / "coll20261004-beat-a.json").write_text(json.dumps(
            {"selection_type": "beat", "selection": ["A"], "stages": {"start": "new"},
             "vouchers": []}), encoding="utf-8")
        (s / "coll20261004-beat-a-installments.json").write_text(json.dumps(
            [{"bill_no": "1"}]), encoding="utf-8")
        loaded = coll_store.load_staged_coll_reports()
        self.assertEqual([p.name for p, _ in loaded], ["coll20261004-beat-a.json"])


class CadenceWebTests(ApiTestCase):
    """Routes against the live app: beat cadence form, pipeline and due pages,
    and salesman scoping. Reuses the ApiTestCase server and seed helpers."""

    def setUp(self):
        super().setUp()
        self._add_user("dist1", "distributor", "pw-dist")
        self._add_user("sm1", "salesman", "pw-sm1")
        self._add_user("sm2", "salesman", "pw-sm2")
        self._add_beat("North", "sm1")
        self._add_beat("South", "sm2")
        self.dist = self._login("dist1", "pw-dist")

    def _beat_row(self, name):
        return next(b for b in coll_store.load_beats_raw() if b["name"] == name)

    def test_create_beat_with_weekly_cadence(self):
        status, body = self._post(self.dist, "/manage/beats/new", {
            "name": "East", "salesman": "sm1", "cadence": "weekly", "cadence_day": "fri",
            "cadence_start": ""})
        self.assertEqual(status, 200)
        row = self._beat_row("East")
        self.assertEqual((row["cadence"], row["cadence_day"]), ("weekly", "fri"))

    def test_weekly_beat_without_day_is_rejected_with_message(self):
        status, body = self._post(self.dist, "/manage/beats/new", {
            "name": "Bad", "salesman": "sm1", "cadence": "weekly", "cadence_day": ""})
        self.assertIn("collection day", body)
        self.assertFalse(any(b["name"] == "Bad" for b in coll_store.load_beats_raw()))

    def test_edit_beat_changes_cadence_and_keeps_a_matching_start_date(self):
        # Oct 9, 2026 is the 2nd Friday of October -> consistent with cadence_week=2.
        status, _ = self._post(self.dist, "/manage/beats/North/edit", {
            "salesman": "sm1", "cadence": "monthly", "cadence_day": "fri", "cadence_week": "2",
            "cadence_start": "2026-10-09"})
        self.assertEqual(status, 200)
        row = self._beat_row("North")
        self.assertEqual(
            (row["cadence"], row["cadence_day"], row["cadence_week"], row["cadence_start"]),
            ("monthly", "fri", "2", "2026-10-09"))

    def test_edit_beat_drops_week_when_switching_away_from_monthly(self):
        status, _ = self._post(self.dist, "/manage/beats/North/edit", {
            "salesman": "sm1", "cadence": "weekly", "cadence_day": "fri", "cadence_week": "2",
            "cadence_start": ""})
        self.assertEqual(status, 200)
        row = self._beat_row("North")
        self.assertEqual((row["cadence"], row["cadence_day"], row["cadence_week"]),
                         ("weekly", "fri", ""))  # cadence_week dropped: irrelevant outside monthly

    def test_salesman_only_edit_keeps_cadence(self):
        coll_store.update_beat_cadence("North", "biweekly", "", "", "2026-10-20")
        status, _ = self._post(self.dist, "/manage/beats/North/edit", {"salesman": "sm2"})
        self.assertEqual(status, 200)
        row = self._beat_row("North")
        self.assertEqual((row["salesman"], row["cadence"], row["cadence_start"]),
                         ("sm2", "biweekly", "2026-10-20"))

    def test_due_page_lists_beat_without_history_inside_window(self):
        # Weekly beat whose collection day is today: guarantees the computed
        # next-due date is today regardless of which real calendar date the
        # test suite happens to run on (monthly has no directly-settable
        # "due today" shortcut now that it's weekday + week-of-month based).
        today_day = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][date.today().weekday()]
        conn = coll_store.get_db()
        try:
            with conn:
                conn.execute("UPDATE beats SET cadence='weekly', cadence_day=? WHERE name='North'",
                             (today_day,))
        finally:
            conn.close()
        status, body = self._get(self.dist, "/reports/due?days=3")
        self.assertEqual(status, 200)
        self.assertIn("North", body)

    def test_pipeline_page_shows_stage_named_like_the_menu(self):
        self._write_staging_report("coll20261001-beat_salesman-North_sm1", "North", "sm1",
                                   start="new")
        status, body = self._get(self.dist, "/reports/pipeline")
        self.assertEqual(status, 200)
        self.assertIn("Approve Collection List", body)
        self.assertIn("North", body)
        # Cross-links to the (now separate) onboarding report are fine; the
        # onboarding stage list itself must not be on this page.
        self.assertNotIn("Pending review", body)

    def test_salesman_pipeline_hides_other_salesmen_beats(self):
        self._write_staging_report("coll20261001-beat_salesman-North_sm1", "North", "sm1",
                                   start="new")
        self._write_staging_report(
            "coll20261001-beat_salesman-South_sm2", "South", "sm2", start="new",
            vouchers=[{"bill_no": "700", "date": "2026-01-01", "balance": "9.00",
                       "payment": "", "payment_date": "", "beat": "South", "salesman": "sm2"}])
        sm1 = self._login("sm1", "pw-sm1")
        status, body = self._get(sm1, "/reports/pipeline")
        self.assertEqual(status, 200)
        self.assertIn("North", body)
        self.assertNotIn("South", body)

    def test_onboarding_pipeline_is_its_own_report(self):
        json_path = self.tmp / "staging" / "addv20261001-dist1.json"
        json_path.write_text(json.dumps({
            "created_by": "dist1", "created_at": "2026-10-01T09:00:00", "flags": [],
            "vouchers": [{"bill_no": "900", "salesman": "sm1", "balance": "10.00",
                         "review_status": "pending"}],
        }), encoding="utf-8")
        status, body = self._get(self.dist, "/reports/onboarding-pipeline")
        self.assertEqual(status, 200)
        self.assertIn("Pending review", body)
        self.assertIn("dist1", body)
        self.assertNotIn("Collection lists", body)


class SetupGapsWebTests(ApiTestCase):
    """Route-level: permission gate, menu visibility, and that the page
    reflects real DB/staging state. Pure-logic coverage of build_setup_gaps
    itself lives in BuildSetupGapsTests above."""

    def setUp(self):
        super().setUp()
        self._add_user("dist1", "distributor", "pw-dist")
        self._add_user("sup1", "supervisor", "pw-sup")
        self._add_user("sm1", "salesman", "pw-sm1")
        self._add_beat("North", "sm1")
        self.dist = self._login("dist1", "pw-dist")

    def test_distributor_sees_the_report(self):
        status, body = self._get(self.dist, "/reports/setup-gaps")
        self.assertEqual(status, 200)
        self.assertIn("Setup Gaps", body)

    def test_supervisor_and_salesman_are_denied(self):
        sup = self._login("sup1", "pw-sup")
        sm = self._login("sm1", "pw-sm1")
        status, body = self._get(sup, "/reports/setup-gaps")
        self.assertIn("have permission", body)
        status, body = self._get(sm, "/reports/setup-gaps")
        self.assertIn("have permission", body)

    def test_menu_card_only_shown_to_distributor(self):
        status, body = self._get(self.dist, "/reports")
        self.assertIn("Setup Gaps", body)
        sup = self._login("sup1", "pw-sup")
        status, body = self._get(sup, "/reports")
        self.assertNotIn("Setup Gaps", body)

    def test_flags_beat_without_salesman(self):
        self._add_beat("South", "")
        status, body = self._get(self.dist, "/reports/setup-gaps")
        self.assertEqual(status, 200)
        self.assertIn("South", body)

    def test_beat_with_voucher_is_not_a_data_feed_gap(self):
        self._add_voucher("1", "North", "sm1")
        status, body = self._get(self.dist, "/reports/setup-gaps")
        self.assertEqual(status, 200)
        self.assertIn("Every beat has at least one voucher", body)


class SalesmanMismatchGuardTests(ApiTestCase):
    """Beat/salesman mismatch guard: blocked at import, and blocked (with an
    auto-raised Amendment Request) at Generate Collection List."""

    def setUp(self):
        super().setUp()
        self._add_user("dist1", "distributor", "pw-dist")
        self._add_user("sm1", "salesman", "pw-sm1")
        self._add_user("sm2", "salesman", "pw-sm2")
        self._add_beat("North", "sm1")
        self.dist = self._login("dist1", "pw-dist")

    def test_import_rejects_voucher_whose_salesman_does_not_match_beat(self):
        csv_body = "bill_no,date,amount,beat,salesman\r\n501,2026-01-01,100.00,North,sm2\r\n"
        boundary = "X" * 10
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="vouchers_file"; filename="v.csv"\r\n'
            f"Content-Type: text/csv\r\n\r\n{csv_body}\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        req = urllib.request.Request(
            self.base + "/coll/import-vouchers", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            resp = self.dist.open(req, timeout=5)
            status, text = resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode()
        self.assertEqual(status, 200)
        self.assertIn("does not match", text)
        self.assertEqual(coll_store.load_addv_batches(), [])

    def test_generate_blocked_and_amendment_request_raised_for_mismatch(self):
        self._add_voucher("601", "North", "sm2", balance="40.00")
        status, body = self._post(self.dist, "/coll/start/generate", {"beat": "North"})
        self.assertEqual(status, 200)
        self.assertIn("Check Amendment Requests", body)
        self.assertIn("blocks generating this list", body)
        open_reqs = [r for r in coll_store.load_amendment_requests() if r["status"] == "open"]
        self.assertEqual([r["bill_no"] for r in open_reqs], ["601"])
        self.assertEqual(open_reqs[0]["requested_by"], "dist1")
        self.assertIn("North", open_reqs[0]["note"])
        # No staging report was created for the blocked beat.
        self.assertEqual(coll_store.load_staged_coll_reports(), [])

    def test_generate_does_not_raise_a_second_open_request(self):
        self._add_voucher("602", "North", "sm2", balance="40.00")
        self._post(self.dist, "/coll/start/generate", {"beat": "North"})
        self._post(self.dist, "/coll/start/generate", {"beat": "North"})
        open_reqs = [r for r in coll_store.load_amendment_requests()
                    if r["status"] == "open" and r["bill_no"] == "602"]
        self.assertEqual(len(open_reqs), 1)

    def test_generate_succeeds_once_mismatch_is_fixed(self):
        self._add_voucher("603", "North", "sm1", balance="40.00")
        status, body = self._post(self.dist, "/coll/start/generate", {"beat": "North"})
        self.assertEqual(status, 200)
        self.assertNotIn("Check Amendment Requests", body)
        self.assertEqual(len(coll_store.load_staged_coll_reports()), 1)


if __name__ == "__main__":
    unittest.main()
