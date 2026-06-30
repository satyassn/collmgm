"""
Unit tests for scripts/coll_store.py

Run:  python -m unittest discover -s tests -v
  or: python -m pytest tests/ -v   (if pytest is available)
"""

import csv
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import coll_store


# ---------------------------------------------------------------------------
# Base class: fresh temp dir + coll_store path patches per test
# ---------------------------------------------------------------------------

class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        (self.tmp / "data").mkdir()
        (self.tmp / "staging").mkdir()
        (self.tmp / "archive").mkdir()

        self._patches = [
            patch.object(coll_store, "DATA_DIR",    self.tmp / "data"),
            patch.object(coll_store, "STAGING_DIR", self.tmp / "staging"),
            patch.object(coll_store, "ARCHIVE_DIR", self.tmp / "archive"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()

    def _write_staging_json(self, name, data):
        path = self.tmp / "staging" / name
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    def _write_csv(self, subdir, name, fieldnames, rows):
        path = self.tmp / subdir / name
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        return path


# ---------------------------------------------------------------------------
# sanitize_filename_component
# ---------------------------------------------------------------------------

class TestSanitize(unittest.TestCase):
    def test_alphanumeric_unchanged(self):
        self.assertEqual(coll_store.sanitize_filename_component("beat1"), "beat1")

    def test_spaces_become_underscores(self):
        self.assertEqual(coll_store.sanitize_filename_component("my beat"), "my_beat")

    def test_special_chars_replaced(self):
        result = coll_store.sanitize_filename_component("a/b:c*d")
        self.assertNotIn("/", result)
        self.assertNotIn(":", result)
        self.assertNotIn("*", result)

    def test_empty_string_returns_unknown(self):
        self.assertEqual(coll_store.sanitize_filename_component(""), "unknown")

    def test_strips_leading_trailing_whitespace(self):
        self.assertEqual(coll_store.sanitize_filename_component("  ok  "), "ok")


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

class TestPasswordHashing(unittest.TestCase):
    def test_round_trip(self):
        h = coll_store.hash_password("secret123")
        self.assertTrue(coll_store._verify_password(h, "secret123"))

    def test_wrong_password_fails(self):
        h = coll_store.hash_password("secret123")
        self.assertFalse(coll_store._verify_password(h, "wrongpass"))

    def test_malformed_hash_returns_false(self):
        self.assertFalse(coll_store._verify_password("notahash", "anything"))

    def test_different_hashes_for_same_password(self):
        h1 = coll_store.hash_password("abc")
        h2 = coll_store.hash_password("abc")
        # different salts → different stored hashes
        self.assertNotEqual(h1, h2)
        self.assertTrue(coll_store._verify_password(h1, "abc"))
        self.assertTrue(coll_store._verify_password(h2, "abc"))


# ---------------------------------------------------------------------------
# verify_user
# ---------------------------------------------------------------------------

class TestVerifyUser(StoreTestCase):
    def _make_users_csv(self, rows):
        path = self.tmp / "data" / "users.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["name", "role", "password_hash"])
            w.writeheader()
            w.writerows(rows)

    def test_valid_salesman(self):
        self._make_users_csv([
            {"name": "alice", "role": "salesman",
             "password_hash": coll_store.hash_password("pw1")},
        ])
        user = coll_store.verify_user("alice", "pw1")
        self.assertIsNotNone(user)
        self.assertEqual(user.name, "alice")
        self.assertEqual(user.role, "salesman")

    def test_wrong_password_returns_none(self):
        self._make_users_csv([
            {"name": "bob", "role": "supervisor",
             "password_hash": coll_store.hash_password("correct")},
        ])
        self.assertIsNone(coll_store.verify_user("bob", "wrong"))

    def test_unknown_user_returns_none(self):
        self._make_users_csv([
            {"name": "carol", "role": "salesman",
             "password_hash": coll_store.hash_password("x")},
        ])
        self.assertIsNone(coll_store.verify_user("nobody", "x"))

    def test_system_role_excluded(self):
        self._make_users_csv([
            {"name": "sys", "role": "system",
             "password_hash": coll_store.hash_password("pw")},
        ])
        self.assertIsNone(coll_store.verify_user("sys", "pw"))

    def test_distributor_role_accepted(self):
        self._make_users_csv([
            {"name": "dan", "role": "distributor",
             "password_hash": coll_store.hash_password("dp")},
        ])
        user = coll_store.verify_user("dan", "dp")
        self.assertIsNotNone(user)
        self.assertEqual(user.role, "distributor")

    def test_missing_users_file_returns_none(self):
        self.assertIsNone(coll_store.verify_user("any", "any"))


# ---------------------------------------------------------------------------
# _load_pending_start_reports  (stages.start == "new")
# ---------------------------------------------------------------------------

class TestLoadPendingStartReports(StoreTestCase):
    def _report(self, stages):
        return {"stages": stages, "selection_type": "beat_salesman",
                "selection": ["b1", "s1"], "vouchers": []}

    def test_start_new_included(self):
        self._write_staging_json("coll_new.json", self._report({"start": "new"}))
        results = coll_store._load_pending_start_reports()
        self.assertEqual(len(results), 1)

    def test_start_confirmed_excluded(self):
        self._write_staging_json("coll_conf.json", self._report({"start": "confirmed"}))
        self.assertEqual(coll_store._load_pending_start_reports(), [])

    def test_submit_in_progress_excluded(self):
        self._write_staging_json("coll_sub.json",
            self._report({"start": "confirmed", "submit": "inprogress"}))
        self.assertEqual(coll_store._load_pending_start_reports(), [])

    def test_no_stages_key_excluded(self):
        self._write_staging_json("coll_bare.json",
            {"selection_type": "beat_salesman", "selection": ["b1", "s1"], "vouchers": []})
        self.assertEqual(coll_store._load_pending_start_reports(), [])

    def test_empty_staging_dir(self):
        self.assertEqual(coll_store._load_pending_start_reports(), [])

    def test_addv_files_ignored(self):
        # addv* files must not be returned
        path = self.tmp / "staging" / "addv20260620-user.json"
        with path.open("w") as f:
            json.dump({"stages": {"start": "new"}}, f)
        self.assertEqual(coll_store._load_pending_start_reports(), [])

    def test_multiple_new_reports_all_returned(self):
        for i in range(3):
            self._write_staging_json(f"coll_r{i}.json", self._report({"start": "new"}))
        self.assertEqual(len(coll_store._load_pending_start_reports()), 3)


# ---------------------------------------------------------------------------
# _load_pending_submit_reports  (stages.submit == "submitted")
# ---------------------------------------------------------------------------

class TestLoadPendingSubmitReports(StoreTestCase):
    def _report(self, stages):
        return {"stages": stages, "selection_type": "beat_salesman",
                "selection": ["b1", "s1"], "vouchers": []}

    def test_submitted_included(self):
        self._write_staging_json("coll_sub.json",
            self._report({"start": "confirmed", "submit": "submitted"}))
        self.assertEqual(len(coll_store._load_pending_submit_reports()), 1)

    def test_inprogress_excluded(self):
        self._write_staging_json("coll_inp.json",
            self._report({"start": "confirmed", "submit": "inprogress"}))
        self.assertEqual(coll_store._load_pending_submit_reports(), [])

    def test_submit_confirmed_excluded(self):
        self._write_staging_json("coll_sconf.json",
            self._report({"start": "confirmed", "submit": "confirmed"}))
        self.assertEqual(coll_store._load_pending_submit_reports(), [])

    def test_no_submit_key_excluded(self):
        self._write_staging_json("coll_bare.json",
            self._report({"start": "confirmed"}))
        self.assertEqual(coll_store._load_pending_submit_reports(), [])

    def test_empty_staging(self):
        self.assertEqual(coll_store._load_pending_submit_reports(), [])


# ---------------------------------------------------------------------------
# _save_installments / _load_installments  (2-tuple, no __status__)
# ---------------------------------------------------------------------------

class TestInstallmentsSidecar(StoreTestCase):
    def _report_path(self):
        return self.tmp / "staging" / "coll_test.json"

    def _vouchers(self, payments):
        return [{"bill_no": bn, "payment": pay} for bn, pay in payments.items()]

    def test_round_trip_basic(self):
        path = self._report_path()
        vouchers = self._vouchers({"B001": "100.00", "B002": "50.00"})
        coll_store._save_installments(path, vouchers)
        data, bookmark = coll_store._load_installments(path)
        self.assertEqual(data["B001"]["payment"], "100.00")
        self.assertEqual(data["B002"]["payment"], "50.00")
        self.assertIsNone(bookmark)

    def test_round_trip_preserves_payment_date(self):
        path = self._report_path()
        vouchers = [{"bill_no": "B001", "payment": "100.00", "payment_date": "2026-06-20"}]
        coll_store._save_installments(path, vouchers)
        data, _ = coll_store._load_installments(path)
        self.assertEqual(data["B001"]["date"], "2026-06-20")

    def test_bookmark_preserved(self):
        path = self._report_path()
        vouchers = self._vouchers({"B001": "10.00"})
        coll_store._save_installments(path, vouchers, bookmark_bill_no="B001")
        data, bookmark = coll_store._load_installments(path)
        self.assertEqual(bookmark, "B001")

    def test_no_status_field_written(self):
        path = self._report_path()
        coll_store._save_installments(path, self._vouchers({"B1": "1.00"}))
        raw = json.loads((path.parent / f"{path.stem}-installments.json").read_text())
        self.assertNotIn("__status__", raw)

    def test_empty_payments_skipped(self):
        path = self._report_path()
        vouchers = [{"bill_no": "B1", "payment": ""}, {"bill_no": "B2", "payment": "5.00"}]
        coll_store._save_installments(path, vouchers)
        data, _ = coll_store._load_installments(path)
        self.assertNotIn("B1", data)
        self.assertIn("B2", data)

    def test_missing_sidecar_returns_empty(self):
        path = self._report_path()
        data, bookmark = coll_store._load_installments(path)
        self.assertEqual(data, {})
        self.assertIsNone(bookmark)

    def test_legacy_status_field_discarded(self):
        path = self._report_path()
        sidecar = path.parent / f"{path.stem}-installments.json"
        sidecar.write_text(json.dumps({"B1": "5.00", "__status__": "complete"}))
        data, _ = coll_store._load_installments(path)
        self.assertNotIn("__status__", data)
        self.assertIn("B1", data)

    def test_legacy_flat_string_normalized_to_dict(self):
        path = self._report_path()
        sidecar = path.parent / f"{path.stem}-installments.json"
        sidecar.write_text(json.dumps({"B1": "5.00"}))
        data, _ = coll_store._load_installments(path)
        self.assertEqual(data["B1"], {"payment": "5.00", "date": ""})


# ---------------------------------------------------------------------------
# _append_installments_csv  (dedup on bill_no + date)
# ---------------------------------------------------------------------------

class TestAppendInstallmentsCSV(StoreTestCase):
    _FIELDS = ["bill_no", "date", "amount", "salesman", "created_by", "created_at"]

    def _vouchers(self, *bill_payments):
        return [{"bill_no": bn, "payment": pay, "salesman": "sm1"}
                for bn, pay in bill_payments]

    def _read_installments(self):
        path = self.tmp / "data" / "installments.csv"
        if not path.exists():
            return []
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_creates_file_with_header(self):
        coll_store._append_installments_csv(self._vouchers(("B001", "100.00")))
        rows = self._read_installments()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bill_no"], "B001")

    def test_appends_to_existing_file(self):
        self._write_csv("data", "installments.csv", self._FIELDS,
                        [{"bill_no": "B000", "date": "2026-01-01", "amount": "50.00",
                          "salesman": "sm0", "created_by": "app", "created_at": "t"}])
        coll_store._append_installments_csv(self._vouchers(("B001", "10.00")))
        rows = self._read_installments()
        self.assertEqual(len(rows), 2)

    def test_duplicate_bill_no_and_date_skipped(self):
        from unittest.mock import patch as mp
        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y-%m-%d")
        self._write_csv("data", "installments.csv", self._FIELDS,
                        [{"bill_no": "B001", "date": today, "amount": "10.00",
                          "salesman": "sm1", "created_by": "app", "created_at": "t"}])
        coll_store._append_installments_csv(self._vouchers(("B001", "10.00")))
        rows = self._read_installments()
        self.assertEqual(len(rows), 1)

    def test_same_bill_different_date_allowed(self):
        self._write_csv("data", "installments.csv", self._FIELDS,
                        [{"bill_no": "B001", "date": "2026-01-01", "amount": "10.00",
                          "salesman": "sm1", "created_by": "app", "created_at": "t"}])
        coll_store._append_installments_csv(self._vouchers(("B001", "20.00")))
        rows = self._read_installments()
        self.assertEqual(len(rows), 2)

    def test_zero_payment_skipped(self):
        coll_store._append_installments_csv(self._vouchers(("B001", "0"), ("B002", "5.00")))
        rows = self._read_installments()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bill_no"], "B002")

    def test_empty_payment_skipped(self):
        coll_store._append_installments_csv([{"bill_no": "B1", "payment": "", "salesman": "s"}])
        self.assertEqual(self._read_installments(), [])

    def test_no_rows_no_file_created(self):
        coll_store._append_installments_csv([])
        self.assertFalse((self.tmp / "data" / "installments.csv").exists())

    def test_uses_voucher_payment_date_not_today(self):
        vouchers = [{"bill_no": "B001", "payment": "10.00", "salesman": "sm1",
                     "payment_date": "2026-06-20"}]
        coll_store._append_installments_csv(vouchers)
        rows = self._read_installments()
        self.assertEqual(rows[0]["date"], "2026-06-20")

    def test_missing_payment_date_falls_back_to_today(self):
        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y-%m-%d")
        coll_store._append_installments_csv(self._vouchers(("B001", "10.00")))
        rows = self._read_installments()
        self.assertEqual(rows[0]["date"], today)

    def test_dedup_keyed_on_per_voucher_date(self):
        self._write_csv("data", "installments.csv", self._FIELDS,
                        [{"bill_no": "B001", "date": "2026-06-20", "amount": "10.00",
                          "salesman": "sm1", "created_by": "app", "created_at": "t"}])
        vouchers = [{"bill_no": "B001", "payment": "10.00", "salesman": "sm1",
                     "payment_date": "2026-06-20"}]
        coll_store._append_installments_csv(vouchers)
        rows = self._read_installments()
        self.assertEqual(len(rows), 1)


# ---------------------------------------------------------------------------
# _update_vouchers_balance  (atomic write, lock, zero detection)
# ---------------------------------------------------------------------------

class TestUpdateVouchersBalance(StoreTestCase):
    _V_FIELDS = ["bill_no", "date", "amount", "balance", "beat", "salesman",
                 "created_by", "created_at"]

    def _make_vouchers_csv(self, rows):
        self._write_csv("data", "vouchers.csv", self._V_FIELDS, rows)

    def _read_vouchers(self):
        path = self.tmp / "data" / "vouchers.csv"
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_balance_reduced_by_payment(self):
        self._make_vouchers_csv([
            {"bill_no": "B001", "date": "2026-01-01", "amount": "500.00",
             "balance": "500.00", "beat": "b1", "salesman": "s1",
             "created_by": "app", "created_at": "t"},
        ])
        vouchers = [{"bill_no": "B001", "payment": "200.00"}]
        coll_store._update_vouchers_balance(vouchers)
        rows = self._read_vouchers()
        self.assertEqual(rows[0]["balance"], "300.00")

    def test_balance_floored_at_zero(self):
        self._make_vouchers_csv([
            {"bill_no": "B001", "date": "2026-01-01", "amount": "100.00",
             "balance": "50.00", "beat": "b1", "salesman": "s1",
             "created_by": "app", "created_at": "t"},
        ])
        vouchers = [{"bill_no": "B001", "payment": "200.00"}]
        coll_store._update_vouchers_balance(vouchers)
        self.assertEqual(self._read_vouchers()[0]["balance"], "0.00")

    def test_zero_balance_returned_as_completed(self):
        self._make_vouchers_csv([
            {"bill_no": "B001", "date": "2026-01-01", "amount": "100.00",
             "balance": "100.00", "beat": "b1", "salesman": "s1",
             "created_by": "app", "created_at": "t"},
        ])
        completed = coll_store._update_vouchers_balance([{"bill_no": "B001", "payment": "100.00"}])
        self.assertIn("B001", completed)

    def test_partial_payment_not_in_completed(self):
        self._make_vouchers_csv([
            {"bill_no": "B001", "date": "2026-01-01", "amount": "100.00",
             "balance": "100.00", "beat": "b1", "salesman": "s1",
             "created_by": "app", "created_at": "t"},
        ])
        completed = coll_store._update_vouchers_balance([{"bill_no": "B001", "payment": "50.00"}])
        self.assertEqual(completed, [])

    def test_no_tmp_file_left_after_success(self):
        self._make_vouchers_csv([
            {"bill_no": "B1", "date": "d", "amount": "10", "balance": "10",
             "beat": "b", "salesman": "s", "created_by": "a", "created_at": "t"},
        ])
        coll_store._update_vouchers_balance([{"bill_no": "B1", "payment": "5"}])
        self.assertFalse((self.tmp / "data" / "vouchers.tmp").exists())

    def test_lock_file_cleaned_up_after_success(self):
        self._make_vouchers_csv([
            {"bill_no": "B1", "date": "d", "amount": "10", "balance": "10",
             "beat": "b", "salesman": "s", "created_by": "a", "created_at": "t"},
        ])
        coll_store._update_vouchers_balance([{"bill_no": "B1", "payment": "5"}])
        self.assertFalse((self.tmp / "data" / ".vouchers.lock").exists())

    def test_existing_lock_raises_runtime_error(self):
        self._make_vouchers_csv([
            {"bill_no": "B1", "date": "d", "amount": "10", "balance": "10",
             "beat": "b", "salesman": "s", "created_by": "a", "created_at": "t"},
        ])
        (self.tmp / "data" / ".vouchers.lock").touch()
        with self.assertRaises(RuntimeError):
            coll_store._update_vouchers_balance([{"bill_no": "B1", "payment": "5"}])

    def test_missing_vouchers_csv_raises(self):
        with self.assertRaises(FileNotFoundError):
            coll_store._update_vouchers_balance([{"bill_no": "B1", "payment": "5"}])

    def test_empty_payment_map_returns_empty_list(self):
        self._make_vouchers_csv([
            {"bill_no": "B1", "date": "d", "amount": "10", "balance": "10",
             "beat": "b", "salesman": "s", "created_by": "a", "created_at": "t"},
        ])
        result = coll_store._update_vouchers_balance([{"bill_no": "B1", "payment": ""}])
        self.assertEqual(result, [])

    def test_unrelated_vouchers_untouched(self):
        self._make_vouchers_csv([
            {"bill_no": "B001", "date": "d", "amount": "100", "balance": "100",
             "beat": "b", "salesman": "s", "created_by": "a", "created_at": "t"},
            {"bill_no": "B002", "date": "d", "amount": "200", "balance": "200",
             "beat": "b", "salesman": "s", "created_by": "a", "created_at": "t"},
        ])
        coll_store._update_vouchers_balance([{"bill_no": "B001", "payment": "50"}])
        rows = {r["bill_no"]: r for r in self._read_vouchers()}
        self.assertEqual(rows["B002"]["balance"], "200")


# ---------------------------------------------------------------------------
# acquire_beat_lock / release_beat_lock
# ---------------------------------------------------------------------------

class TestBeatLock(StoreTestCase):
    def test_first_acquire_succeeds(self):
        self.assertTrue(coll_store.acquire_beat_lock("beat1"))

    def test_second_acquire_same_beat_fails(self):
        coll_store.acquire_beat_lock("beat1")
        self.assertFalse(coll_store.acquire_beat_lock("beat1"))

    def test_different_beats_independent(self):
        self.assertTrue(coll_store.acquire_beat_lock("beat1"))
        self.assertTrue(coll_store.acquire_beat_lock("beat2"))

    def test_release_allows_reacquire(self):
        coll_store.acquire_beat_lock("beat1")
        coll_store.release_beat_lock("beat1")
        self.assertTrue(coll_store.acquire_beat_lock("beat1"))

    def test_release_without_lock_is_safe(self):
        # Should not raise even if lock doesn't exist
        coll_store.release_beat_lock("nonexistent_beat")

    def test_lock_file_exists_after_acquire(self):
        coll_store.acquire_beat_lock("mybeat")
        lock_files = list((self.tmp / "staging").glob(".beatlock-*.lock"))
        self.assertEqual(len(lock_files), 1)

    def test_lock_file_gone_after_release(self):
        coll_store.acquire_beat_lock("mybeat")
        coll_store.release_beat_lock("mybeat")
        lock_files = list((self.tmp / "staging").glob(".beatlock-*.lock"))
        self.assertEqual(len(lock_files), 0)


# ---------------------------------------------------------------------------
# Finalize checkpoint
# ---------------------------------------------------------------------------

class TestFinalizeCheckpoint(StoreTestCase):
    def test_write_then_read(self):
        fake_path = self.tmp / "staging" / "coll_test.json"
        coll_store.write_finalize_checkpoint(fake_path, 3)
        cp = coll_store.read_finalize_checkpoint()
        self.assertIsNotNone(cp)
        self.assertEqual(cp["step"], 3)
        self.assertIn("coll_test.json", cp["report"])

    def test_clear_removes_checkpoint(self):
        coll_store.write_finalize_checkpoint(self.tmp / "x.json", 1)
        coll_store.clear_finalize_checkpoint()
        self.assertIsNone(coll_store.read_finalize_checkpoint())

    def test_read_when_missing_returns_none(self):
        self.assertIsNone(coll_store.read_finalize_checkpoint())

    def test_overwrite_updates_step(self):
        coll_store.write_finalize_checkpoint(self.tmp / "x.json", 1)
        coll_store.write_finalize_checkpoint(self.tmp / "x.json", 4)
        self.assertEqual(coll_store.read_finalize_checkpoint()["step"], 4)


# ---------------------------------------------------------------------------
# load_vouchers_raw
# ---------------------------------------------------------------------------

class TestLoadVouchersRaw(StoreTestCase):
    _FIELDS = ["bill_no", "date", "amount", "balance", "beat", "salesman",
               "created_by", "created_at"]

    def test_reads_rows(self):
        self._write_csv("data", "vouchers.csv", self._FIELDS,
                        [{"bill_no": "B001", "date": "2026-01-01", "amount": "100",
                          "balance": "100", "beat": "b1", "salesman": "s1",
                          "created_by": "app", "created_at": "t"}])
        rows = coll_store.load_vouchers_raw()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bill_no"], "B001")

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            coll_store.load_vouchers_raw()

    def test_returns_all_rows(self):
        data = [{"bill_no": f"B{i:03}", "date": "2026-01-01", "amount": "10",
                 "balance": "10", "beat": "b1", "salesman": "s1",
                 "created_by": "app", "created_at": "t"} for i in range(5)]
        self._write_csv("data", "vouchers.csv", self._FIELDS, data)
        self.assertEqual(len(coll_store.load_vouchers_raw()), 5)


# ---------------------------------------------------------------------------
# _archive_completed
# ---------------------------------------------------------------------------

class TestArchiveCompleted(StoreTestCase):
    _V_FIELDS = ["bill_no", "date", "amount", "balance", "beat", "salesman",
                 "created_by", "created_at"]
    _I_FIELDS = ["bill_no", "date", "amount", "salesman", "created_by", "created_at"]

    def _setup_data(self, vouchers, installments=None):
        self._write_csv("data", "vouchers.csv", self._V_FIELDS, vouchers)
        if installments:
            self._write_csv("data", "installments.csv", self._I_FIELDS, installments)

    def _read_csv(self, subdir, name, fields):
        path = self.tmp / subdir / name
        if not path.exists():
            return []
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def _row(self, bn, balance="0.00"):
        return {"bill_no": bn, "date": "2026-01-01", "amount": "100",
                "balance": balance, "beat": "b1", "salesman": "s1",
                "created_by": "app", "created_at": "t"}

    def test_completed_voucher_moved(self):
        self._setup_data([self._row("B001"), self._row("B002", "50.00")])
        coll_store._archive_completed(["B001"])
        remaining = self._read_csv("data", "vouchers.csv", self._V_FIELDS)
        completed = self._read_csv("data", "completed_vouchers.csv", self._V_FIELDS)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["bill_no"], "B002")
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["bill_no"], "B001")

    def test_non_completed_voucher_stays(self):
        self._setup_data([self._row("B001"), self._row("B002")])
        coll_store._archive_completed(["B001"])
        remaining = self._read_csv("data", "vouchers.csv", self._V_FIELDS)
        self.assertTrue(any(r["bill_no"] == "B002" for r in remaining))

    def test_installments_archived_too(self):
        self._setup_data(
            [self._row("B001")],
            [{"bill_no": "B001", "date": "2026-01-01", "amount": "100",
              "salesman": "s1", "created_by": "app", "created_at": "t"}]
        )
        coll_store._archive_completed(["B001"])
        inst = self._read_csv("data", "installments.csv", self._I_FIELDS)
        comp_inst = self._read_csv("data", "completed_installments.csv", self._I_FIELDS)
        self.assertEqual(inst, [])
        self.assertEqual(len(comp_inst), 1)

    def test_empty_bill_nos_is_noop(self):
        self._setup_data([self._row("B001")])
        coll_store._archive_completed([])
        self.assertEqual(len(self._read_csv("data", "vouchers.csv", self._V_FIELDS)), 1)
        self.assertFalse((self.tmp / "data" / "completed_vouchers.csv").exists())

    def test_appends_to_existing_completed(self):
        self._setup_data([self._row("B002")])
        # Pre-existing completed
        self._write_csv("data", "completed_vouchers.csv", self._V_FIELDS, [self._row("B001")])
        coll_store._archive_completed(["B002"])
        completed = self._read_csv("data", "completed_vouchers.csv", self._V_FIELDS)
        self.assertEqual(len(completed), 2)


if __name__ == "__main__":
    unittest.main()
