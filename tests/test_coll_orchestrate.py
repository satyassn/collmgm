"""
Unit tests for scripts/coll_orchestrate.py

Run:  python -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import coll_data
import coll_orchestrate
import coll_store


class OrchestrateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        (self.tmp / "staging").mkdir()
        (self.tmp / "archive").mkdir()

        # STAGING_DIR is imported by value into coll_store, coll_data, and
        # coll_orchestrate independently — each needs its own patch. ARCHIVE_DIR
        # is only used inside coll_store's own archive_files(), so one patch
        # suffices there, but it's still essential: without it, archive_files()
        # would try to move files into the real project's archive/ directory.
        self._patches = [
            patch.object(coll_store, "STAGING_DIR", self.tmp / "staging"),
            patch.object(coll_data, "STAGING_DIR", self.tmp / "staging"),
            patch.object(coll_orchestrate, "STAGING_DIR", self.tmp / "staging"),
            patch.object(coll_store, "ARCHIVE_DIR", self.tmp / "archive"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()

    def _report(self, stage_submit="submitted", beat="beat1", salesman="sm1"):
        return {
            "stages": {"start": "confirmed", "submit": stage_submit, "post": ""},
            "selection_type": "beat_salesman",
            "selection": [beat, salesman],
            "vouchers": [
                {"bill_no": "20", "date": "2026-01-01", "balance": "50.00", "payment": "10.00",
                 "beat": beat, "salesman": salesman},
                {"bill_no": "10", "date": "2026-01-01", "balance": "100.00", "payment": "",
                 "beat": beat, "salesman": salesman},
            ],
        }

    def _write_json(self, name, data):
        path = self.tmp / name
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    def _write_staging_json(self, name, data):
        return self._write_json(Path("staging") / name, data)


class TestPrepareSubmitReview(OrchestrateTestCase):
    def test_sorts_vouchers_by_bill_no(self):
        report_path = self._write_json("coll1.json", self._report())
        data = self._report()
        result = coll_orchestrate.prepare_submit_review(report_path, data)
        self.assertEqual([v["bill_no"] for v in result["vouchers"]], ["10", "20"])

    def test_regenerates_txt_sidecar(self):
        report_path = self._write_json("coll1.json", self._report())
        data = self._report()
        coll_orchestrate.prepare_submit_review(report_path, data)
        txt_path = report_path.with_suffix(".txt")
        self.assertTrue(txt_path.exists())
        self.assertIn("COLLECTION LIST", txt_path.read_text(encoding="utf-8"))

    def test_txt_write_failure_is_swallowed(self):
        # Directory instead of a writable file path -> write_collection_text raises;
        # prepare_submit_review must not propagate the error.
        report_path = self.tmp / "coll1.json"
        report_path.write_text("{}", encoding="utf-8")
        bad_txt = report_path.with_suffix(".txt")
        bad_txt.mkdir()
        data = self._report()
        result = coll_orchestrate.prepare_submit_review(report_path, data)
        self.assertEqual([v["bill_no"] for v in result["vouchers"]], ["10", "20"])


class TestApplySubmitApproval(OrchestrateTestCase):
    def test_approve_sets_confirmed_and_persists(self):
        data = self._report()
        report_path = self._write_json("coll1.json", data)
        coll_orchestrate.apply_submit_approval(report_path, data, "approve")
        self.assertEqual(data["stages"]["submit"], "confirmed")
        persisted = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["stages"]["submit"], "confirmed")

    def test_return_sets_returned_and_persists(self):
        data = self._report()
        report_path = self._write_json("coll1.json", data)
        coll_orchestrate.apply_submit_approval(report_path, data, "return")
        self.assertEqual(data["stages"]["submit"], "returned")
        persisted = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["stages"]["submit"], "returned")

    def test_missing_stages_key_is_created(self):
        data = self._report()
        del data["stages"]
        report_path = self._write_json("coll1.json", data)
        coll_orchestrate.apply_submit_approval(report_path, data, "approve")
        self.assertEqual(data["stages"]["submit"], "confirmed")


class TestCheckActiveBeatReport(OrchestrateTestCase):
    def _start_report(self, stages):
        return {
            "stages": stages,
            "selection_type": "beat_salesman",
            "selection": ["beat1", "sm1"],
            "vouchers": [{"bill_no": "1", "balance": "10.00"}],
        }

    def test_no_report_returns_none(self):
        state, path, data = coll_orchestrate.check_active_beat_report("beat_salesman", ["beat1", "sm1"])
        self.assertEqual(state, coll_orchestrate.ActiveReportState.NONE)
        self.assertIsNone(path)
        self.assertIsNone(data)

    def test_pending_start_state(self):
        self._write_staging_json("coll1.json", self._start_report({"start": "new"}))
        state, path, data = coll_orchestrate.check_active_beat_report("beat_salesman", ["beat1", "sm1"])
        self.assertEqual(state, coll_orchestrate.ActiveReportState.PENDING_START)
        self.assertIsNotNone(path)

    def test_start_confirmed_state(self):
        self._write_staging_json("coll1.json", self._start_report({"start": "confirmed", "submit": ""}))
        state, _, _ = coll_orchestrate.check_active_beat_report("beat_salesman", ["beat1", "sm1"])
        self.assertEqual(state, coll_orchestrate.ActiveReportState.START_CONFIRMED)

    def test_in_submit_pipeline_state(self):
        self._write_staging_json("coll1.json", self._start_report({"start": "confirmed", "submit": "submitted"}))
        state, _, _ = coll_orchestrate.check_active_beat_report("beat_salesman", ["beat1", "sm1"])
        self.assertEqual(state, coll_orchestrate.ActiveReportState.IN_SUBMIT_PIPELINE)

    def test_in_submit_pipeline_takes_priority_over_start_confirmed(self):
        self._write_staging_json("coll1.json", self._start_report({"start": "confirmed", "submit": "inprogress"}))
        state, _, _ = coll_orchestrate.check_active_beat_report("beat_salesman", ["beat1", "sm1"])
        self.assertEqual(state, coll_orchestrate.ActiveReportState.IN_SUBMIT_PIPELINE)

    def test_different_beat_not_matched(self):
        self._write_staging_json("coll1.json", self._start_report({"start": "new"}))
        state, _, _ = coll_orchestrate.check_active_beat_report("beat_salesman", ["other_beat", "sm1"])
        self.assertEqual(state, coll_orchestrate.ActiveReportState.NONE)


class TestGenerateCollectionList(OrchestrateTestCase):
    def _vouchers(self):
        return [{"bill_no": "1", "balance": "10.00", "voucher_date": "2026-01-01", "payment": ""}]

    def test_creates_report_and_acquires_lock(self):
        outcome = coll_orchestrate.generate_collection_list("beat1", "sm1", self._vouchers())
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.json_path.exists())
        self.assertTrue(outcome.txt_path.exists())
        persisted = json.loads(outcome.json_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["stages"], {"start": "new", "submit": "", "post": ""})
        self.assertEqual(persisted["selection"], ["beat1", "sm1"])
        # Lock acquired -> a second acquire for the same beat must fail.
        self.assertFalse(coll_store.acquire_beat_lock("beat1"))

    def test_lock_conflict_returns_not_ok(self):
        self.assertTrue(coll_store.acquire_beat_lock("beat1"))
        outcome = coll_orchestrate.generate_collection_list("beat1", "sm1", self._vouchers())
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, "lock_conflict")
        self.assertIsNone(outcome.json_path)


class TestApproveStartStage(OrchestrateTestCase):
    def _pending_report(self, vouchers=None):
        return {
            "stages": {"start": "new", "submit": "", "post": ""},
            "selection_type": "beat_salesman",
            "selection": ["beat1", "sm1"],
            "vouchers": vouchers if vouchers is not None else [{"bill_no": "1", "balance": "10.00"}],
        }

    def test_approve_sets_confirmed_and_regenerates_txt(self):
        data = self._pending_report()
        report_path = self._write_staging_json("coll1.json", data)
        result = coll_orchestrate.approve_start_stage(report_path, data)
        self.assertEqual(result["stages"]["start"], "confirmed")
        txt_path = report_path.with_suffix(".txt")
        self.assertTrue(txt_path.exists())
        persisted = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["stages"]["start"], "confirmed")

    def test_approve_with_no_vouchers_skips_txt_without_raising(self):
        data = self._pending_report(vouchers=[])
        report_path = self._write_staging_json("coll1.json", data)
        result = coll_orchestrate.approve_start_stage(report_path, data)
        self.assertEqual(result["stages"]["start"], "confirmed")
        self.assertFalse(report_path.with_suffix(".txt").exists())


class TestApplyStartApproval(OrchestrateTestCase):
    def _generated_report(self):
        outcome = coll_orchestrate.generate_collection_list(
            "beat1", "sm1", [{"bill_no": "1", "balance": "10.00"}])
        data = json.loads(outcome.json_path.read_text(encoding="utf-8"))
        return outcome.json_path, data

    def test_approve_delegates_to_approve_start_stage(self):
        report_path, data = self._generated_report()
        coll_orchestrate.apply_start_approval(report_path, data, "approve")
        self.assertEqual(data["stages"]["start"], "confirmed")

    def test_return_deletes_staging_and_releases_lock(self):
        report_path, data = self._generated_report()
        coll_orchestrate.apply_start_approval(report_path, data, "return")
        self.assertFalse(report_path.exists())
        # Lock released -> re-acquiring the same beat now succeeds.
        self.assertTrue(coll_store.acquire_beat_lock("beat1"))

    def test_cancel_deletes_staging_and_releases_lock(self):
        report_path, data = self._generated_report()
        coll_orchestrate.apply_start_approval(report_path, data, "cancel")
        self.assertFalse(report_path.exists())
        self.assertTrue(coll_store.acquire_beat_lock("beat1"))


class TestComputePaymentDates(OrchestrateTestCase):
    def test_empty_payment_clears_date(self):
        vouchers = [{"bill_no": "1", "payment": ""}]
        coll_orchestrate.compute_payment_dates(vouchers, {}, today="2026-02-01")
        self.assertEqual(vouchers[0]["payment_date"], "")

    def test_new_payment_stamped_today(self):
        vouchers = [{"bill_no": "1", "payment": "10.00"}]
        coll_orchestrate.compute_payment_dates(vouchers, {}, today="2026-02-01")
        self.assertEqual(vouchers[0]["payment_date"], "2026-02-01")

    def test_unchanged_payment_keeps_original_date(self):
        # Re-editing a report without changing an already-recorded payment must
        # NOT bump its date — this is the fix applied to the web path, which
        # previously (pre-unification) always re-stamped today's date.
        vouchers = [{"bill_no": "1", "payment": "10.00"}]
        prior = {"1": {"payment": "10.00", "date": "2026-01-15"}}
        coll_orchestrate.compute_payment_dates(vouchers, prior, today="2026-02-01")
        self.assertEqual(vouchers[0]["payment_date"], "2026-01-15")

    def test_changed_payment_stamped_today(self):
        vouchers = [{"bill_no": "1", "payment": "20.00"}]
        prior = {"1": {"payment": "10.00", "date": "2026-01-15"}}
        coll_orchestrate.compute_payment_dates(vouchers, prior, today="2026-02-01")
        self.assertEqual(vouchers[0]["payment_date"], "2026-02-01")


class TestRecordSubmitPayments(OrchestrateTestCase):
    def _report(self):
        return {
            "stages": {"start": "confirmed", "submit": "", "post": ""},
            "selection_type": "beat_salesman",
            "selection": ["beat1", "sm1"],
            "vouchers": [{"bill_no": "1", "balance": "10.00", "payment": ""}],
        }

    def test_inprogress_save_persists_installments_sidecar(self):
        data = self._report()
        report_path = self._write_staging_json("coll1.json", data)
        vouchers = [{"bill_no": "1", "balance": "10.00", "payment": "5.00", "payment_date": "2026-02-01"}]
        coll_orchestrate.record_submit_payments(report_path, data, vouchers, submit_for_review=False)
        self.assertEqual(data["stages"]["submit"], "inprogress")
        inst_path = report_path.parent / f"{report_path.stem}-installments.json"
        self.assertTrue(inst_path.exists())
        saved = json.loads(inst_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["1"]["payment"], "5.00")
        self.assertFalse(report_path.with_suffix(".txt").exists())

    def test_inprogress_save_stores_bookmark(self):
        data = self._report()
        report_path = self._write_staging_json("coll1.json", data)
        vouchers = [{"bill_no": "1", "balance": "10.00", "payment": "5.00", "payment_date": "2026-02-01"}]
        coll_orchestrate.record_submit_payments(report_path, data, vouchers, submit_for_review=False,
                                                bookmark_bill_no="1")
        inst_path = report_path.parent / f"{report_path.stem}-installments.json"
        saved = json.loads(inst_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["__bookmark__"], "1")

    def test_submit_for_review_sets_submitted_and_writes_txt(self):
        data = self._report()
        report_path = self._write_staging_json("coll1.json", data)
        vouchers = [{"bill_no": "1", "balance": "10.00", "payment": "5.00", "payment_date": "2026-02-01"}]
        coll_orchestrate.record_submit_payments(report_path, data, vouchers, submit_for_review=True,
                                                beats=["beat1"], salesmen=["sm1"])
        self.assertEqual(data["stages"]["submit"], "submitted")
        self.assertTrue(report_path.with_suffix(".txt").exists())
        persisted = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["stages"]["submit"], "submitted")
        self.assertEqual(persisted["vouchers"], vouchers)

    def test_submit_for_review_with_no_vouchers_skips_txt(self):
        data = self._report()
        report_path = self._write_staging_json("coll1.json", data)
        coll_orchestrate.record_submit_payments(report_path, data, [], submit_for_review=True,
                                                beats=["beat1"], salesmen=["sm1"])
        self.assertEqual(data["stages"]["submit"], "submitted")
        self.assertFalse(report_path.with_suffix(".txt").exists())


class PostTestCase(OrchestrateTestCase):
    """Adds a real SQLite DB (needed by post_confirmed_report's DB writes)."""

    def setUp(self):
        super().setUp()
        (self.tmp / "data").mkdir()
        self._db_patches = [patch.object(coll_store, "DATA_DIR", self.tmp / "data")]
        for p in self._db_patches:
            p.start()
        coll_store.init_db()

    def tearDown(self):
        for p in self._db_patches:
            p.stop()
        super().tearDown()

    def _insert_voucher(self, bill_no="1", balance="100.00", beat="beat1", salesman="sm1"):
        conn = coll_store.get_db()
        try:
            conn.execute(
                "INSERT INTO vouchers (bill_no, date, amount, balance, beat, salesman, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (bill_no, "2026-01-01", "100.00", balance, beat, salesman, "app", "t"),
            )
            conn.commit()
        finally:
            conn.close()

    def _query(self, sql, params=()):
        conn = coll_store.get_db()
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def _post_report(self, bill_no="1", payment="100.00", beat="beat1", salesman="sm1"):
        return {
            "stages": {"start": "confirmed", "submit": "confirmed", "post": ""},
            "selection_type": "beat_salesman",
            "selection": [beat, salesman],
            "vouchers": [{"bill_no": bill_no, "balance": "100.00", "payment": payment,
                          "payment_date": "2026-02-01", "beat": beat, "salesman": salesman}],
        }


class TestPostConfirmedReport(PostTestCase):
    def test_full_payment_archives_voucher_and_confirms(self):
        self._insert_voucher(balance="100.00")
        data = self._post_report(payment="100.00")
        report_path = self._write_staging_json("coll1.json", data)

        outcome = coll_orchestrate.post_confirmed_report(report_path, data)

        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.completed_bill_nos, ["1"])
        self.assertEqual(outcome.total_collected, coll_orchestrate.Decimal("100.00"))
        self.assertEqual(outcome.paid_count, 1)
        self.assertEqual(data["stages"]["post"], "confirmed")
        self.assertEqual(self._query("SELECT * FROM vouchers WHERE bill_no = '1'"), [])
        completed = self._query("SELECT * FROM completed_vouchers WHERE bill_no = '1'")
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["balance"], "0.00")
        # Staging files archived out of STAGING_DIR.
        self.assertFalse(report_path.exists())
        self.assertIsNone(coll_store.read_finalize_checkpoint())

    def test_partial_payment_keeps_voucher_open(self):
        self._insert_voucher(balance="100.00")
        data = self._post_report(payment="40.00")
        report_path = self._write_staging_json("coll1.json", data)

        outcome = coll_orchestrate.post_confirmed_report(report_path, data)

        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.completed_bill_nos, [])
        remaining = self._query("SELECT * FROM vouchers WHERE bill_no = '1'")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["balance"], "60.00")

    def test_failure_during_balance_update_leaves_checkpoint(self):
        self._insert_voucher(balance="100.00")
        data = self._post_report(payment="100.00")
        report_path = self._write_staging_json("coll1.json", data)

        with patch.object(coll_orchestrate, "_update_vouchers_balance", side_effect=RuntimeError("boom")):
            outcome = coll_orchestrate.post_confirmed_report(report_path, data)

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.step_failed, 2)
        self.assertEqual(outcome.error, "boom")
        # Checkpoint left in place for manual recovery; staging file untouched.
        self.assertIsNotNone(coll_store.read_finalize_checkpoint())
        self.assertTrue(report_path.exists())

    def test_failure_saving_json_after_db_writes_succeed(self):
        # The riskiest edge case: DB already updated, but the staging JSON
        # (stages.post='confirmed') failed to save — must be surfaced, not silent.
        self._insert_voucher(balance="100.00")
        data = self._post_report(payment="100.00")
        report_path = self._write_staging_json("coll1.json", data)

        with patch.object(coll_orchestrate, "save_report_json", side_effect=RuntimeError("disk full")):
            outcome = coll_orchestrate.post_confirmed_report(report_path, data)

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.step_failed, 5)
        self.assertEqual(outcome.error, "disk full")
        # DB writes already committed despite the later failure.
        self.assertEqual(self._query("SELECT * FROM vouchers WHERE bill_no = '1'"), [])
        self.assertEqual(len(self._query("SELECT * FROM completed_vouchers WHERE bill_no = '1'")), 1)

    def test_archive_failure_is_non_fatal_but_reported(self):
        self._insert_voucher(balance="100.00")
        data = self._post_report(payment="100.00")
        report_path = self._write_staging_json("coll1.json", data)

        with patch.object(coll_orchestrate, "archive_files", side_effect=RuntimeError("locked")):
            outcome = coll_orchestrate.post_confirmed_report(report_path, data)

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.archive_warning, "locked")
        self.assertIsNone(coll_store.read_finalize_checkpoint())

    def test_beat_lock_released_after_successful_post(self):
        self._insert_voucher(balance="100.00")
        self.assertTrue(coll_store.acquire_beat_lock("beat1"))
        data = self._post_report(payment="100.00")
        report_path = self._write_staging_json("coll1.json", data)

        outcome = coll_orchestrate.post_confirmed_report(report_path, data)

        self.assertTrue(outcome.ok)
        self.assertTrue(coll_store.acquire_beat_lock("beat1"))


class TestReturnPostStage(PostTestCase):
    def test_sets_submit_returned_to_submitted_and_persists(self):
        data = self._post_report()
        report_path = self._write_staging_json("coll1.json", data)
        result = coll_orchestrate.return_post_stage(report_path, data)
        self.assertEqual(result["stages"]["submit"], "submitted")
        persisted = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["stages"]["submit"], "submitted")


if __name__ == "__main__":
    unittest.main()
