"""
Unit tests for scripts/coll_store.py

Run:  python -m unittest discover -s tests -v
"""

import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import coll_store


# ---------------------------------------------------------------------------
# Base class: fresh temp dir + patched path constants + initialised SQLite DB
# ---------------------------------------------------------------------------

class StoreTestCase(unittest.TestCase):
    def setUp(self):
        import tempfile
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

        # Fresh SQLite DB in the temp data dir (DATA_DIR is already patched)
        coll_store.init_db()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _insert_rows(self, table, rows):
        conn = coll_store.get_db()
        try:
            for row in rows:
                cols = list(row.keys())
                ph   = ", ".join("?" * len(cols))
                conn.execute(
                    f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({ph})",
                    [row[c] for c in cols],
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

    def _write_staging_json(self, name, data):
        path = self.tmp / "staging" / name
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    # ------------------------------------------------------------------
    # Voucher / installment row factories
    # ------------------------------------------------------------------

    def _v_row(self, bill_no, balance="100.00", beat="b1", salesman="s1"):
        return {"bill_no": bill_no, "date": "2026-01-01", "amount": "100.00",
                "balance": balance, "beat": beat, "salesman": salesman,
                "created_by": "app", "created_at": "t"}

    def _i_row(self, bill_no, date="2026-01-01", amount="10.00", salesman="s1"):
        return {"bill_no": bill_no, "date": date, "amount": amount,
                "salesman": salesman, "created_by": "app", "created_at": "t"}


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
        self.assertNotEqual(h1, h2)
        self.assertTrue(coll_store._verify_password(h1, "abc"))
        self.assertTrue(coll_store._verify_password(h2, "abc"))


# ---------------------------------------------------------------------------
# verify_user
# ---------------------------------------------------------------------------

class TestVerifyUser(StoreTestCase):
    def _add_user(self, name, role, password):
        self._insert_rows("users", [{"name": name, "role": role,
                                      "password_hash": coll_store.hash_password(password)}])

    def test_valid_salesman(self):
        self._add_user("alice", "salesman", "pw1")
        user = coll_store.verify_user("alice", "pw1")
        self.assertIsNotNone(user)
        self.assertEqual(user.name, "alice")
        self.assertEqual(user.role, "salesman")

    def test_wrong_password_returns_none(self):
        self._add_user("bob", "supervisor", "correct")
        self.assertIsNone(coll_store.verify_user("bob", "wrong"))

    def test_unknown_user_returns_none(self):
        self._add_user("carol", "salesman", "x")
        self.assertIsNone(coll_store.verify_user("nobody", "x"))

    def test_system_role_excluded(self):
        self._add_user("sys", "system", "pw")
        self.assertIsNone(coll_store.verify_user("sys", "pw"))

    def test_distributor_role_accepted(self):
        self._add_user("dan", "distributor", "dp")
        user = coll_store.verify_user("dan", "dp")
        self.assertIsNotNone(user)
        self.assertEqual(user.role, "distributor")

    def test_empty_users_table_returns_none(self):
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
        self.assertEqual(len(coll_store._load_pending_start_reports()), 1)

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
# _save_installments / _load_installments  (sidecar JSON — unchanged)
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
# _append_installments_csv  (now writes to SQLite installments table)
# ---------------------------------------------------------------------------

class TestAppendInstallmentsCSV(StoreTestCase):
    def _vouchers(self, *bill_payments):
        return [{"bill_no": bn, "payment": pay, "salesman": "sm1"}
                for bn, pay in bill_payments]

    def _read_installments(self):
        return self._query("SELECT * FROM installments")

    def test_inserts_row_to_db(self):
        coll_store._append_installments_csv(self._vouchers(("B001", "100.00")))
        rows = self._read_installments()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bill_no"], "B001")

    def test_appends_to_existing_rows(self):
        self._insert_rows("installments",
                          [self._i_row("B000", date="2026-01-01", amount="50.00")])
        coll_store._append_installments_csv(self._vouchers(("B001", "10.00")))
        rows = self._read_installments()
        self.assertEqual(len(rows), 2)

    def test_duplicate_bill_no_and_date_skipped(self):
        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y-%m-%d")
        self._insert_rows("installments",
                          [self._i_row("B001", date=today, amount="10.00")])
        coll_store._append_installments_csv(self._vouchers(("B001", "10.00")))
        rows = self._read_installments()
        self.assertEqual(len(rows), 1)

    def test_same_bill_different_date_allowed(self):
        self._insert_rows("installments",
                          [self._i_row("B001", date="2026-01-01", amount="10.00")])
        vouchers = [{"bill_no": "B001", "payment": "20.00", "salesman": "sm1",
                     "payment_date": "2026-06-20"}]
        coll_store._append_installments_csv(vouchers)
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

    def test_no_rows_nothing_inserted(self):
        coll_store._append_installments_csv([])
        self.assertEqual(self._read_installments(), [])

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
        self._insert_rows("installments",
                          [self._i_row("B001", date="2026-06-20", amount="10.00")])
        vouchers = [{"bill_no": "B001", "payment": "10.00", "salesman": "sm1",
                     "payment_date": "2026-06-20"}]
        coll_store._append_installments_csv(vouchers)
        rows = self._read_installments()
        self.assertEqual(len(rows), 1)


# ---------------------------------------------------------------------------
# _update_vouchers_balance
# ---------------------------------------------------------------------------

class TestUpdateVouchersBalance(StoreTestCase):
    def _make_vouchers(self, rows):
        self._insert_rows("vouchers", rows)

    def _read_vouchers(self):
        return self._query("SELECT * FROM vouchers")

    def test_balance_reduced_by_payment(self):
        self._make_vouchers([self._v_row("B001", balance="500.00")])
        coll_store._update_vouchers_balance([{"bill_no": "B001", "payment": "200.00"}])
        rows = self._read_vouchers()
        self.assertEqual(rows[0]["balance"], "300.00")

    def test_balance_floored_at_zero(self):
        self._make_vouchers([self._v_row("B001", balance="50.00")])
        coll_store._update_vouchers_balance([{"bill_no": "B001", "payment": "200.00"}])
        self.assertEqual(self._read_vouchers()[0]["balance"], "0.00")

    def test_zero_balance_returned_as_completed(self):
        self._make_vouchers([self._v_row("B001", balance="100.00")])
        completed = coll_store._update_vouchers_balance([{"bill_no": "B001", "payment": "100.00"}])
        self.assertIn("B001", completed)

    def test_partial_payment_not_in_completed(self):
        self._make_vouchers([self._v_row("B001", balance="100.00")])
        completed = coll_store._update_vouchers_balance([{"bill_no": "B001", "payment": "50.00"}])
        self.assertEqual(completed, [])

    def test_missing_db_raises(self):
        # Remove the DB so the function can detect it's missing
        coll_store._db_path().unlink()
        with self.assertRaises(FileNotFoundError):
            coll_store._update_vouchers_balance([{"bill_no": "B1", "payment": "5"}])

    def test_empty_payment_map_returns_empty_list(self):
        self._make_vouchers([self._v_row("B1")])
        result = coll_store._update_vouchers_balance([{"bill_no": "B1", "payment": ""}])
        self.assertEqual(result, [])

    def test_unrelated_vouchers_untouched(self):
        self._make_vouchers([self._v_row("B001", balance="100.00"),
                              self._v_row("B002", balance="200.00")])
        coll_store._update_vouchers_balance([{"bill_no": "B001", "payment": "50"}])
        rows = {r["bill_no"]: r for r in self._read_vouchers()}
        self.assertEqual(rows["B002"]["balance"], "200.00")


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
    def test_reads_rows(self):
        self._insert_rows("vouchers", [self._v_row("B001")])
        rows = coll_store.load_vouchers_raw()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bill_no"], "B001")

    def test_missing_db_raises(self):
        coll_store._db_path().unlink()
        with self.assertRaises(FileNotFoundError):
            coll_store.load_vouchers_raw()

    def test_returns_all_rows(self):
        self._insert_rows("vouchers", [self._v_row(f"B{i:03}") for i in range(5)])
        self.assertEqual(len(coll_store.load_vouchers_raw()), 5)


# ---------------------------------------------------------------------------
# _archive_completed
# ---------------------------------------------------------------------------

class TestArchiveCompleted(StoreTestCase):
    def test_completed_voucher_moved(self):
        self._insert_rows("vouchers", [self._v_row("B001"), self._v_row("B002", balance="50.00")])
        coll_store._archive_completed(["B001"])
        remaining  = self._query("SELECT bill_no FROM vouchers")
        completed  = self._query("SELECT bill_no FROM completed_vouchers")
        self.assertEqual([r["bill_no"] for r in remaining], ["B002"])
        self.assertEqual([r["bill_no"] for r in completed], ["B001"])

    def test_non_completed_voucher_stays(self):
        self._insert_rows("vouchers", [self._v_row("B001"), self._v_row("B002")])
        coll_store._archive_completed(["B001"])
        remaining = self._query("SELECT bill_no FROM vouchers")
        self.assertTrue(any(r["bill_no"] == "B002" for r in remaining))

    def test_installments_archived_too(self):
        self._insert_rows("vouchers",      [self._v_row("B001")])
        self._insert_rows("installments",  [self._i_row("B001")])
        coll_store._archive_completed(["B001"])
        self.assertEqual(self._query("SELECT * FROM installments"), [])
        self.assertEqual(len(self._query("SELECT * FROM completed_installments")), 1)

    def test_empty_bill_nos_is_noop(self):
        self._insert_rows("vouchers", [self._v_row("B001")])
        coll_store._archive_completed([])
        self.assertEqual(len(self._query("SELECT * FROM vouchers")), 1)
        self.assertEqual(self._query("SELECT * FROM completed_vouchers"), [])

    def test_appends_to_existing_completed(self):
        self._insert_rows("vouchers",           [self._v_row("B002")])
        self._insert_rows("completed_vouchers", [self._v_row("B001")])
        coll_store._archive_completed(["B002"])
        completed = self._query("SELECT bill_no FROM completed_vouchers")
        self.assertEqual(len(completed), 2)


if __name__ == "__main__":
    unittest.main()
