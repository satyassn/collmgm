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


class DbTestCase(OrchestrateTestCase):
    """Adds a real SQLite DB (needed wherever staged reports are re-validated
    against master data, and by post_confirmed_report's DB writes)."""

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


class TestApplySubmitApproval(DbTestCase):
    def setUp(self):
        super().setUp()
        # Master rows matching the _report() fixture — approval now
        # cross-checks every staged voucher against current master data.
        self._insert_voucher(bill_no="20", balance="50.00")
        self._insert_voucher(bill_no="10", balance="100.00")

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

    def test_missing_stages_key_is_rejected(self):
        # A report with no stages block is not in the 'submitted' stage, so
        # approving it must fail the stage guard rather than invent the key.
        data = self._report()
        del data["stages"]
        report_path = self._write_json("coll1.json", data)
        with self.assertRaises(coll_orchestrate.StageError):
            coll_orchestrate.apply_submit_approval(report_path, data, "approve")

    def test_wrong_stage_is_rejected(self):
        for submit_stage in ("", "inprogress", "returned", "confirmed"):
            data = self._report(stage_submit=submit_stage)
            report_path = self._write_json("coll1.json", data)
            with self.assertRaises(coll_orchestrate.StageError):
                coll_orchestrate.apply_submit_approval(report_path, data, "approve")
            # Nothing persisted — the file still holds the original stage.
            persisted = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["stages"]["submit"], submit_stage)

    def test_approve_rejects_payment_over_balance(self):
        # Defense in depth: data staged by an unvalidated client must stop
        # at the supervisor gate, not flow on toward the master tables.
        data = self._report()
        data["vouchers"][0]["payment"] = "50.01"  # balance is 50.00
        report_path = self._write_json("coll1.json", data)
        with self.assertRaises(coll_orchestrate.ValidationError) as ctx:
            coll_orchestrate.apply_submit_approval(report_path, data, "approve")
        self.assertIn("20: exceeds balance", str(ctx.exception))
        persisted = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["stages"]["submit"], "submitted")

    def test_approve_rejects_non_numeric_payment(self):
        data = self._report()
        data["vouchers"][0]["payment"] = "abc"
        report_path = self._write_json("coll1.json", data)
        with self.assertRaises(coll_orchestrate.ValidationError):
            coll_orchestrate.apply_submit_approval(report_path, data, "approve")

    def test_return_allowed_despite_invalid_payments(self):
        # Returning IS the remedy for bad data — it must never be blocked.
        data = self._report()
        data["vouchers"][0]["payment"] = "99999.00"
        report_path = self._write_json("coll1.json", data)
        coll_orchestrate.apply_submit_approval(report_path, data, "return")
        self.assertEqual(data["stages"]["submit"], "returned")


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


class PostTestCase(DbTestCase):
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

        outcome = coll_orchestrate.post_confirmed_report(report_path)

        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.completed_bill_nos, ["1"])
        self.assertEqual(outcome.total_collected, coll_orchestrate.Decimal("100.00"))
        self.assertEqual(outcome.paid_count, 1)
        # post_confirmed_report re-reads the report inside its claim, so the
        # confirmed stage lands in the (archived) file, not the caller's dict.
        archived = json.loads((self.tmp / "archive" / "coll1.json").read_text(encoding="utf-8"))
        self.assertEqual(archived["stages"]["post"], "confirmed")
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

        outcome = coll_orchestrate.post_confirmed_report(report_path)

        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.completed_bill_nos, [])
        remaining = self._query("SELECT * FROM vouchers WHERE bill_no = '1'")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["balance"], "60.00")

    def test_db_failure_rolls_back_and_clears_checkpoint(self):
        self._insert_voucher(balance="100.00")
        data = self._post_report(payment="100.00")
        report_path = self._write_staging_json("coll1.json", data)

        with patch.object(coll_orchestrate, "apply_post_to_db", side_effect=RuntimeError("boom")):
            outcome = coll_orchestrate.post_confirmed_report(report_path)

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.step_failed, 1)
        self.assertIn("boom", outcome.error)
        self.assertIn("no changes were written", outcome.error)
        # DB rolled back — checkpoint cleared, staging file untouched.
        self.assertIsNone(coll_store.read_finalize_checkpoint())
        self.assertTrue(report_path.exists())

    def test_partial_failure_rolls_back_all_db_writes(self):
        # Two-voucher report; the second voucher vanishes from master after
        # approval (validation is patched out to simulate the race) — the
        # first voucher's installment and balance change must roll back too.
        self._insert_voucher(bill_no="1", balance="100.00")
        data = self._post_report(payment="40.00")
        data["vouchers"].append({"bill_no": "2", "balance": "100.00", "payment": "10.00",
                                 "payment_date": "2026-02-01", "beat": "beat1",
                                 "salesman": "sm1"})
        report_path = self._write_staging_json("coll1.json", data)

        with patch.object(coll_orchestrate, "validate_staged_report", return_value=[]):
            outcome = coll_orchestrate.post_confirmed_report(report_path)

        self.assertFalse(outcome.ok)
        self.assertIn("2 not found in master", outcome.error)
        self.assertIn("no changes were written", outcome.error)
        self.assertEqual(self._query("SELECT * FROM installments"), [])
        rows = self._query("SELECT balance FROM vouchers WHERE bill_no = '1'")
        self.assertEqual(rows[0]["balance"], "100.00")
        self.assertIsNone(coll_store.read_finalize_checkpoint())

    def test_stale_checkpoint_at_db_commit_blocks_repost(self):
        # A checkpoint at step >= 4 for this report means a previous post
        # reached the DB but never finished — a blind retry would double-post.
        self._insert_voucher(balance="100.00")
        data = self._post_report(payment="40.00")
        report_path = self._write_staging_json("coll1.json", data)
        coll_store.write_finalize_checkpoint(report_path, 4)

        outcome = coll_orchestrate.post_confirmed_report(report_path)

        self.assertFalse(outcome.ok)
        self.assertIn("reached the database", outcome.error)
        self.assertEqual(self._query("SELECT * FROM installments"), [])

        # A different report's checkpoint does not block this one.
        coll_store.write_finalize_checkpoint(self.tmp / "staging" / "other.json", 4)
        outcome = coll_orchestrate.post_confirmed_report(report_path)
        self.assertTrue(outcome.ok, outcome.error)

    def test_failure_saving_json_after_db_writes_succeed(self):
        # The riskiest edge case: DB already updated, but the staging JSON
        # (stages.post='confirmed') failed to save — must be surfaced, not silent.
        self._insert_voucher(balance="100.00")
        data = self._post_report(payment="100.00")
        report_path = self._write_staging_json("coll1.json", data)

        with patch.object(coll_orchestrate, "save_report_json", side_effect=RuntimeError("disk full")):
            outcome = coll_orchestrate.post_confirmed_report(report_path)

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
            outcome = coll_orchestrate.post_confirmed_report(report_path)

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.archive_warning, "locked")
        self.assertIsNone(coll_store.read_finalize_checkpoint())

    def test_beat_lock_released_after_successful_post(self):
        self._insert_voucher(balance="100.00")
        self.assertTrue(coll_store.acquire_beat_lock("beat1"))
        data = self._post_report(payment="100.00")
        report_path = self._write_staging_json("coll1.json", data)

        outcome = coll_orchestrate.post_confirmed_report(report_path)

        self.assertTrue(outcome.ok)
        self.assertTrue(coll_store.acquire_beat_lock("beat1"))


class TestPostStageGuards(PostTestCase):
    def test_unapproved_report_is_not_posted(self):
        self._insert_voucher(balance="100.00")
        for submit_stage in ("", "inprogress", "submitted", "returned"):
            data = self._post_report(payment="40.00")
            data["stages"]["submit"] = submit_stage
            report_path = self._write_staging_json("coll1.json", data)

            outcome = coll_orchestrate.post_confirmed_report(report_path)

            self.assertFalse(outcome.ok)
            self.assertIn("not approved for posting", outcome.error)
            self.assertIsNone(outcome.step_failed)
            # No DB writes happened: balance untouched, no installments.
            rows = self._query("SELECT balance FROM vouchers WHERE bill_no = '1'")
            self.assertEqual(rows[0]["balance"], "100.00")
            self.assertEqual(self._query("SELECT * FROM installments"), [])
            report_path.unlink()

    def test_concurrent_post_claim_fails_fast(self):
        self._insert_voucher(balance="100.00")
        data = self._post_report(payment="40.00")
        report_path = self._write_staging_json("coll1.json", data)

        # Simulate another session mid-post: the claim is held.
        self.assertTrue(coll_store.acquire_post_claim(report_path))
        outcome = coll_orchestrate.post_confirmed_report(report_path)
        self.assertFalse(outcome.ok)
        self.assertIn("already being posted", outcome.error)
        rows = self._query("SELECT balance FROM vouchers WHERE bill_no = '1'")
        self.assertEqual(rows[0]["balance"], "100.00")

        # Once released, the same report posts normally — exactly once.
        coll_store.release_post_claim(report_path)
        outcome = coll_orchestrate.post_confirmed_report(report_path)
        self.assertTrue(outcome.ok, outcome.error)
        rows = self._query("SELECT balance FROM vouchers WHERE bill_no = '1'")
        self.assertEqual(rows[0]["balance"], "60.00")

    def test_payment_over_balance_is_not_posted(self):
        # Final backstop: even a submit-confirmed report is re-validated
        # before anything touches the master tables.
        self._insert_voucher(balance="100.00")
        data = self._post_report(payment="100.01")
        report_path = self._write_staging_json("coll1.json", data)

        outcome = coll_orchestrate.post_confirmed_report(report_path)

        self.assertFalse(outcome.ok)
        self.assertIn("failed validation", outcome.error)
        self.assertIn("exceeds balance", outcome.error)
        rows = self._query("SELECT balance FROM vouchers WHERE bill_no = '1'")
        self.assertEqual(rows[0]["balance"], "100.00")
        self.assertEqual(self._query("SELECT * FROM installments"), [])
        self.assertIsNone(coll_store.read_finalize_checkpoint())
        self.assertTrue(report_path.exists())

    def test_posted_by_recorded_in_installments(self):
        self._insert_voucher(balance="100.00")
        data = self._post_report(payment="40.00")
        report_path = self._write_staging_json("coll1.json", data)

        outcome = coll_orchestrate.post_confirmed_report(report_path, posted_by="dist1")

        self.assertTrue(outcome.ok, outcome.error)
        rows = self._query("SELECT created_by FROM installments WHERE bill_no = '1'")
        self.assertEqual(rows, [{"created_by": "dist1"}])

    def test_claim_released_after_failed_post(self):
        # A failure inside the sequence must not leave the claim behind,
        # or the retry the checkpoint message asks for would be impossible.
        self._insert_voucher(balance="100.00")
        data = self._post_report(payment="100.00")
        report_path = self._write_staging_json("coll1.json", data)

        with patch.object(coll_orchestrate, "apply_post_to_db", side_effect=RuntimeError("boom")):
            outcome = coll_orchestrate.post_confirmed_report(report_path)
        self.assertFalse(outcome.ok)
        self.assertTrue(coll_store.acquire_post_claim(report_path))
        coll_store.release_post_claim(report_path)


class TestReturnPostStage(PostTestCase):
    def test_sets_submit_returned_to_submitted_and_persists(self):
        data = self._post_report()
        report_path = self._write_staging_json("coll1.json", data)
        result = coll_orchestrate.return_post_stage(report_path, data)
        self.assertEqual(result["stages"]["submit"], "submitted")
        persisted = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["stages"]["submit"], "submitted")

    def test_rejects_report_not_awaiting_post(self):
        data = self._post_report()
        data["stages"]["submit"] = "submitted"
        report_path = self._write_staging_json("coll1.json", data)
        with self.assertRaises(coll_orchestrate.StageError):
            coll_orchestrate.return_post_stage(report_path, data)


class TestApplyStartApprovalStageGuard(OrchestrateTestCase):
    def test_rejects_non_new_report(self):
        for action in ("approve", "return", "cancel"):
            data = self._report(stage_submit="submitted")  # start=confirmed
            report_path = self._write_staging_json("coll1.json", data)
            with self.assertRaises(coll_orchestrate.StageError):
                coll_orchestrate.apply_start_approval(report_path, data, action)
            # In particular: cancel must NOT delete a report in the submit pipeline.
            self.assertTrue(report_path.exists())
            report_path.unlink()


class TestRecordSubmitPaymentsStageGuard(OrchestrateTestCase):
    def test_rejects_already_submitted_report(self):
        for submit_stage in ("submitted", "confirmed"):
            data = self._report(stage_submit=submit_stage)
            report_path = self._write_staging_json("coll1.json", data)
            with self.assertRaises(coll_orchestrate.StageError):
                coll_orchestrate.record_submit_payments(report_path, data, data["vouchers"],
                                                        submit_for_review=False)
            # No installments sidecar written for the rejected save.
            sidecar = report_path.parent / f"{report_path.stem}-installments.json"
            self.assertFalse(sidecar.exists())
            report_path.unlink()

    def test_rejects_unapproved_start(self):
        data = self._report(stage_submit="")
        data["stages"]["start"] = "new"
        report_path = self._write_staging_json("coll1.json", data)
        with self.assertRaises(coll_orchestrate.StageError):
            coll_orchestrate.record_submit_payments(report_path, data, data["vouchers"],
                                                    submit_for_review=False)

    def test_allows_resume_stages(self):
        for submit_stage in ("", "inprogress", "returned"):
            data = self._report(stage_submit=submit_stage)
            report_path = self._write_staging_json("coll1.json", data)
            result = coll_orchestrate.record_submit_payments(report_path, data, data["vouchers"],
                                                             submit_for_review=False)
            self.assertEqual(result["stages"]["submit"], "inprogress")
            report_path.unlink()


class TestValidateStagedReport(DbTestCase):
    def _staged(self, **overrides):
        data = self._report()
        data.update(overrides)
        return data

    def setUp(self):
        super().setUp()
        self._insert_voucher(bill_no="20", balance="50.00")
        self._insert_voucher(bill_no="10", balance="100.00")

    def test_valid_report_passes(self):
        self.assertEqual(coll_orchestrate.validate_staged_report(self._staged()), [])

    def test_vouchers_not_a_list(self):
        errors = coll_orchestrate.validate_staged_report(self._staged(vouchers={"a": 1}))
        self.assertEqual(errors, ["report: 'vouchers' is not a list"])

    def test_malformed_entries_reported(self):
        data = self._staged()
        data["vouchers"].append("junk")
        data["vouchers"].append({"balance": "10", "payment": ""})   # no bill_no
        data["vouchers"].append({"bill_no": "20", "payment": ""})   # no balance key
        errors = coll_orchestrate.validate_staged_report(data)
        self.assertEqual(sum("malformed voucher" in e for e in errors), 3)

    def test_voucher_missing_from_master(self):
        data = self._staged()
        data["vouchers"].append({"bill_no": "999", "balance": "10.00", "payment": "",
                                 "beat": "beat1", "salesman": "sm1"})
        errors = coll_orchestrate.validate_staged_report(data)
        self.assertTrue(any("999: not found in master" in e for e in errors))

    def test_payment_validated_against_master_balance_not_staged_copy(self):
        # Staged balance inflated to 500 — a tampered file must not allow a
        # payment beyond the voucher's CURRENT master balance (50.00).
        data = self._staged()
        data["vouchers"][0]["balance"] = "500.00"
        data["vouchers"][0]["payment"] = "60.00"
        errors = coll_orchestrate.validate_staged_report(data)
        self.assertTrue(any("20: exceeds balance (50.00)" in e for e in errors))

    def test_staged_beat_salesman_mismatch(self):
        data = self._staged()
        data["vouchers"][0]["salesman"] = "sm2"
        errors = coll_orchestrate.validate_staged_report(data)
        self.assertTrue(any("20: beat/salesman mismatch" in e for e in errors))

    def test_master_beat_salesman_mismatch(self):
        self._insert_voucher(bill_no="30", balance="10.00", beat="beat2", salesman="sm1")
        data = self._staged()
        data["vouchers"].append({"bill_no": "30", "balance": "10.00", "payment": "",
                                 "beat": "beat1", "salesman": "sm1"})
        errors = coll_orchestrate.validate_staged_report(data)
        self.assertTrue(any("30: beat/salesman mismatch" in e for e in errors))

    def test_duplicate_bill_no_in_report(self):
        data = self._staged()
        data["vouchers"].append(dict(data["vouchers"][0]))
        errors = coll_orchestrate.validate_staged_report(data)
        self.assertTrue(any("appears more than once" in e for e in errors))

    def test_future_and_garbage_payment_dates(self):
        data = self._staged()
        data["vouchers"][0]["payment_date"] = "2999-01-01"
        data["vouchers"][1]["payment_date"] = "not-a-date"
        errors = coll_orchestrate.validate_staged_report(data)
        self.assertTrue(any("payment date '2999-01-01' is in the future" in e for e in errors))
        self.assertTrue(any("invalid payment date 'not-a-date'" in e for e in errors))

    def test_corrupt_master_balance_is_an_error(self):
        # '1.2.3' passes the schema's GLOB check (digits and dots only) but
        # is not a parseable Decimal.
        conn = coll_store.get_db()
        try:
            conn.execute("UPDATE vouchers SET balance = '1.2.3' WHERE bill_no = '20'")
            conn.commit()
        finally:
            conn.close()
        errors = coll_orchestrate.validate_staged_report(self._staged())
        self.assertTrue(any("20: stored balance is invalid" in e for e in errors))

    def test_empty_db_reports_all_missing(self):
        # DB file exists but has no vouchers.
        conn = coll_store.get_db()
        try:
            conn.execute("DELETE FROM vouchers")
            conn.commit()
        finally:
            conn.close()
        errors = coll_orchestrate.validate_staged_report(self._staged())
        self.assertEqual(sum("not found in master" in e for e in errors), 2)


class TestValidatePayment(unittest.TestCase):
    def test_empty_stays_empty(self):
        self.assertEqual(coll_orchestrate.validate_payment("", "50.00"), ("", None))
        self.assertEqual(coll_orchestrate.validate_payment("   ", "50.00"), ("", None))
        self.assertEqual(coll_orchestrate.validate_payment(None, "50.00"), ("", None))

    def test_valid_amount_is_quantized(self):
        self.assertEqual(coll_orchestrate.validate_payment("10", "50.00"), ("10.00", None))
        self.assertEqual(coll_orchestrate.validate_payment(" 10.5 ", "50.00"), ("10.50", None))
        self.assertEqual(coll_orchestrate.validate_payment("50.00", "50.00"), ("50.00", None))

    def test_rejects_non_numbers(self):
        for bad in ("abc", "10x", "NaN", "Infinity", "-Infinity"):
            normalized, reason = coll_orchestrate.validate_payment(bad, "50.00")
            self.assertIsNone(normalized, bad)
            self.assertEqual(reason, "not a number")

    def test_rejects_negative(self):
        normalized, reason = coll_orchestrate.validate_payment("-5", "50.00")
        self.assertIsNone(normalized)
        self.assertEqual(reason, "cannot be negative")

    def test_rejects_over_balance(self):
        normalized, reason = coll_orchestrate.validate_payment("50.01", "50.00")
        self.assertIsNone(normalized)
        self.assertIn("exceeds balance", reason)

    def test_unparseable_balance_rejects_payment(self):
        for bad_balance in ("garbage", "NaN", "Infinity", "-Infinity"):
            normalized, reason = coll_orchestrate.validate_payment("10", bad_balance)
            self.assertIsNone(normalized, bad_balance)
            self.assertIn("stored balance is invalid", reason)


if __name__ == "__main__":
    unittest.main()
