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
# apply_post_to_db  (single-transaction post: installments + balances + archive)
# ---------------------------------------------------------------------------

class TestApplyPostToDb(StoreTestCase):
    def _staged(self, bill_no, payment, **extra):
        v = {"bill_no": bill_no, "payment": payment, "salesman": "s1"}
        v.update(extra)
        return v

    def _read_installments(self):
        return self._query("SELECT * FROM installments")

    def _read_vouchers(self):
        return self._query("SELECT * FROM vouchers")

    def test_inserts_installment_and_reduces_balance(self):
        self._insert_rows("vouchers", [self._v_row("B001", balance="500.00")])
        completed = coll_store.apply_post_to_db([self._staged("B001", "200.00")])
        self.assertEqual(completed, [])
        rows = self._read_installments()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bill_no"], "B001")
        self.assertEqual(self._read_vouchers()[0]["balance"], "300.00")

    def test_two_same_day_installments_both_persist(self):
        # A second genuine payment on the same bill and date must not be
        # silently swallowed (the old UNIQUE(bill_no,date) + INSERT OR IGNORE
        # behaviour) — posting is exactly-once now, so both rows are real.
        self._insert_rows("vouchers", [self._v_row("B001", balance="100.00")])
        coll_store.apply_post_to_db([self._staged("B001", "10.00",
                                                  payment_date="2026-06-20")])
        coll_store.apply_post_to_db([self._staged("B001", "10.00",
                                                  payment_date="2026-06-20")])
        rows = self._read_installments()
        self.assertEqual(len(rows), 2)
        self.assertEqual(self._read_vouchers()[0]["balance"], "80.00")

    def test_zero_and_empty_payments_skipped(self):
        self._insert_rows("vouchers", [self._v_row("B001"), self._v_row("B002"),
                                       self._v_row("B003")])
        completed = coll_store.apply_post_to_db([
            self._staged("B001", "0"),
            self._staged("B002", ""),
            self._staged("B003", "5.00"),
        ])
        self.assertEqual(completed, [])
        rows = self._read_installments()
        self.assertEqual([r["bill_no"] for r in rows], ["B003"])
        vouchers = {r["bill_no"]: r["balance"] for r in self._read_vouchers()}
        self.assertEqual(vouchers["B001"], "100.00")
        self.assertEqual(vouchers["B002"], "100.00")
        self.assertEqual(vouchers["B003"], "95.00")

    def test_no_rows_nothing_inserted(self):
        coll_store.apply_post_to_db([])
        self.assertEqual(self._read_installments(), [])

    def test_uses_voucher_payment_date_not_today(self):
        self._insert_rows("vouchers", [self._v_row("B001")])
        coll_store.apply_post_to_db([self._staged("B001", "10.00",
                                                  payment_date="2026-06-20")])
        self.assertEqual(self._read_installments()[0]["date"], "2026-06-20")

    def test_missing_payment_date_falls_back_to_today(self):
        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y-%m-%d")
        self._insert_rows("vouchers", [self._v_row("B001")])
        coll_store.apply_post_to_db([self._staged("B001", "10.00")])
        self.assertEqual(self._read_installments()[0]["date"], today)

    def test_balance_floored_at_zero(self):
        self._insert_rows("vouchers", [self._v_row("B001", balance="50.00")])
        completed = coll_store.apply_post_to_db([self._staged("B001", "200.00")])
        # Floored to zero => completed and archived.
        self.assertEqual(completed, ["B001"])
        archived = self._query("SELECT balance FROM completed_vouchers")
        self.assertEqual(archived[0]["balance"], "0.00")

    def test_full_payment_archives_voucher_and_installments(self):
        self._insert_rows("vouchers", [self._v_row("B001", balance="100.00")])
        completed = coll_store.apply_post_to_db([self._staged("B001", "100.00")])
        self.assertEqual(completed, ["B001"])
        self.assertEqual(self._read_vouchers(), [])
        self.assertEqual(self._read_installments(), [])
        self.assertEqual(len(self._query("SELECT * FROM completed_vouchers")), 1)
        self.assertEqual(len(self._query("SELECT * FROM completed_installments")), 1)

    def test_missing_db_raises(self):
        coll_store._db_path().unlink()
        with self.assertRaises(FileNotFoundError):
            coll_store.apply_post_to_db([self._staged("B1", "5")])

    def test_unrelated_vouchers_untouched(self):
        self._insert_rows("vouchers", [self._v_row("B001", balance="100.00"),
                                       self._v_row("B002", balance="200.00")])
        coll_store.apply_post_to_db([self._staged("B001", "50")])
        rows = {r["bill_no"]: r for r in self._read_vouchers()}
        self.assertEqual(rows["B002"]["balance"], "200.00")

    def test_invalid_payment_raises_before_any_write(self):
        self._insert_rows("vouchers", [self._v_row("B001"), self._v_row("B002")])
        for bad in ("garbage", "NaN", "Infinity"):
            with self.assertRaises(ValueError):
                coll_store.apply_post_to_db([self._staged("B001", "10.00"),
                                             self._staged("B002", bad)])
            self.assertEqual(self._read_installments(), [], bad)
            vouchers = {r["bill_no"]: r["balance"] for r in self._read_vouchers()}
            self.assertEqual(vouchers["B001"], "100.00", bad)

    def test_missing_voucher_rolls_back_everything(self):
        # Second voucher absent from master: the first voucher's installment
        # and balance change must roll back with it — nothing partial persists.
        self._insert_rows("vouchers", [self._v_row("B001", balance="100.00")])
        with self.assertRaises(ValueError) as ctx:
            coll_store.apply_post_to_db([self._staged("B001", "10.00"),
                                         self._staged("B999", "10.00")])
        self.assertIn("B999", str(ctx.exception))
        self.assertIn("not found in master", str(ctx.exception))
        self.assertEqual(self._read_installments(), [])
        self.assertEqual(self._read_vouchers()[0]["balance"], "100.00")

    def test_corrupt_stored_balance_rolls_back(self):
        # '1.2.3' passes the schema's GLOB check (digits and dots only) but
        # is not a parseable Decimal — the strict write path must reject it.
        self._insert_rows("vouchers", [self._v_row("B001", balance="1.2.3")])
        with self.assertRaises(ValueError) as ctx:
            coll_store.apply_post_to_db([self._staged("B001", "10.00")])
        self.assertIn("invalid stored balance", str(ctx.exception))
        self.assertEqual(self._read_installments(), [])


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
    def _archive(self, bill_nos):
        conn = coll_store.get_db()
        try:
            with conn:
                coll_store._archive_completed(conn, bill_nos)
        finally:
            conn.close()

    def test_completed_voucher_moved(self):
        self._insert_rows("vouchers", [self._v_row("B001"), self._v_row("B002", balance="50.00")])
        self._archive(["B001"])
        remaining  = self._query("SELECT bill_no FROM vouchers")
        completed  = self._query("SELECT bill_no FROM completed_vouchers")
        self.assertEqual([r["bill_no"] for r in remaining], ["B002"])
        self.assertEqual([r["bill_no"] for r in completed], ["B001"])

    def test_non_completed_voucher_stays(self):
        self._insert_rows("vouchers", [self._v_row("B001"), self._v_row("B002")])
        self._archive(["B001"])
        remaining = self._query("SELECT bill_no FROM vouchers")
        self.assertTrue(any(r["bill_no"] == "B002" for r in remaining))

    def test_installments_archived_too(self):
        self._insert_rows("vouchers",      [self._v_row("B001")])
        self._insert_rows("installments",  [self._i_row("B001")])
        self._archive(["B001"])
        self.assertEqual(self._query("SELECT * FROM installments"), [])
        self.assertEqual(len(self._query("SELECT * FROM completed_installments")), 1)

    def test_empty_bill_nos_is_noop(self):
        self._insert_rows("vouchers", [self._v_row("B001")])
        self._archive([])
        self.assertEqual(len(self._query("SELECT * FROM vouchers")), 1)
        self.assertEqual(self._query("SELECT * FROM completed_vouchers"), [])

    def test_appends_to_existing_completed(self):
        self._insert_rows("vouchers",           [self._v_row("B002")])
        self._insert_rows("completed_vouchers", [self._v_row("B001")])
        self._archive(["B002"])
        completed = self._query("SELECT bill_no FROM completed_vouchers")
        self.assertEqual(len(completed), 2)


# ---------------------------------------------------------------------------
# Schema v1 migration
# ---------------------------------------------------------------------------

# The pre-v1 DDL, verbatim, to build legacy databases for migration tests.
_V0_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    name          TEXT PRIMARY KEY,
    role          TEXT NOT NULL,
    password_hash TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS beats (
    name     TEXT PRIMARY KEY,
    salesman TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS permissions (
    role       TEXT NOT NULL,
    action_key TEXT NOT NULL,
    PRIMARY KEY (role, action_key)
);
CREATE TABLE IF NOT EXISTS vouchers (
    bill_no    TEXT PRIMARY KEY,
    date       TEXT NOT NULL,
    amount     TEXT NOT NULL,
    balance    TEXT NOT NULL,
    beat       TEXT NOT NULL,
    salesman   TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS installments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_no    TEXT NOT NULL,
    date       TEXT NOT NULL,
    amount     TEXT NOT NULL,
    salesman   TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    UNIQUE(bill_no, date)
);
CREATE TABLE IF NOT EXISTS completed_vouchers (
    bill_no    TEXT PRIMARY KEY,
    date       TEXT NOT NULL,
    amount     TEXT NOT NULL,
    balance    TEXT NOT NULL,
    beat       TEXT NOT NULL,
    salesman   TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS completed_installments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_no    TEXT NOT NULL,
    date       TEXT NOT NULL,
    amount     TEXT NOT NULL,
    salesman   TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
"""


class TestSchemaMigrationV1(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        (self.tmp / "data").mkdir()
        self._patch = patch.object(coll_store, "DATA_DIR", self.tmp / "data")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmpdir.cleanup()

    def _build_v0_db(self, inserts=()):
        import sqlite3
        conn = sqlite3.connect(str(coll_store._db_path()))
        try:
            conn.executescript(_V0_SCHEMA)
            for sql, params in inserts:
                conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def _user_version(self):
        conn = coll_store.get_db()
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def _query(self, sql, params=()):
        conn = coll_store.get_db()
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def _exec(self, sql, params=()):
        conn = coll_store.get_db()
        try:
            with conn:
                conn.execute(sql, params)
        finally:
            conn.close()

    _VOUCHER = ("INSERT INTO vouchers (bill_no, date, amount, balance, beat, salesman)"
                " VALUES (?, ?, ?, ?, ?, ?)")
    _INSTALLMENT = ("INSERT INTO installments (bill_no, date, amount, salesman)"
                    " VALUES (?, ?, ?, ?)")

    def test_v0_db_migrates_with_data_intact(self):
        self._build_v0_db([
            ("INSERT INTO users (name, role) VALUES (?, ?)", ("sm1", "salesman")),
            ("INSERT INTO beats (name, salesman) VALUES (?, ?)", ("beat1", "sm1")),
            (self._VOUCHER, ("B001", "2026-01-01", "100.00", "60.00", "beat1", "sm1")),
            (self._INSTALLMENT, ("B001", "2026-01-02", "40.00", "sm1")),
            ("INSERT INTO completed_vouchers (bill_no, date, amount, balance, beat, salesman)"
             " VALUES (?, ?, ?, ?, ?, ?)",
             ("B000", "2025-12-01", "50.00", "0.00", "beat1", "sm1")),
        ])
        coll_store.init_db()
        self.assertEqual(self._user_version(), 1)
        self.assertEqual(self._query("SELECT balance FROM vouchers")[0]["balance"], "60.00")
        self.assertEqual(len(self._query("SELECT * FROM installments")), 1)
        self.assertEqual(len(self._query("SELECT * FROM completed_vouchers")), 1)
        self.assertEqual(self._query("SELECT role FROM users")[0]["role"], "salesman")
        # UNIQUE(bill_no,date) is gone: a second same-day installment persists.
        self._exec(self._INSTALLMENT, ("B001", "2026-01-02", "5.00", "sm1"))
        self.assertEqual(len(self._query("SELECT * FROM installments")), 2)

    def test_constraints_enforced_after_migration(self):
        import sqlite3
        self._build_v0_db([(self._VOUCHER,
                            ("B001", "2026-01-01", "100.00", "100.00", "beat1", "sm1"))])
        coll_store.init_db()
        cases = [
            ("INSERT INTO users (name, role) VALUES (?, ?)", ("x", "hacker")),
            ("INSERT INTO users (name, role) VALUES (?, ?)", ("", "salesman")),
            (self._INSTALLMENT, ("B999", "2026-01-01", "10.00", "sm1")),  # orphan FK
            (self._INSTALLMENT, ("B001", "2026-01-01", "nan", "sm1")),
            (self._INSTALLMENT, ("B001", "2026-01-01", "-5", "sm1")),
            (self._VOUCHER, (None, "2026-01-01", "1", "1", "b", "s")),
            (self._VOUCHER, ("B002", "2026-01-01", "Infinity", "1", "b", "s")),
            (self._VOUCHER, ("B003", "", "1", "1", "b", "s")),
        ]
        for sql, params in cases:
            with self.assertRaises(sqlite3.IntegrityError, msg=params):
                self._exec(sql, params)

    def test_dirty_legacy_data_blocks_migration(self):
        self._build_v0_db([
            ("INSERT INTO users (name, role) VALUES (?, ?)", ("x", "hacker")),
        ])
        with self.assertRaises(coll_store.MigrationError) as ctx:
            coll_store.init_db()
        self.assertIn("users[x]", str(ctx.exception))
        self.assertEqual(self._user_version(), 0)
        # DB untouched — the bad row is still there for repair.
        self.assertEqual(self._query("SELECT role FROM users")[0]["role"], "hacker")

    def test_non_numeric_money_blocks_migration(self):
        self._build_v0_db([(self._VOUCHER,
                            ("B001", "2026-01-01", "100.00", "nan", "beat1", "sm1"))])
        with self.assertRaises(coll_store.MigrationError) as ctx:
            coll_store.init_db()
        self.assertIn("non-numeric balance", str(ctx.exception))

    def test_orphan_from_interrupted_archive_auto_repaired(self):
        # Voucher already archived, but its installment was left behind —
        # debris from a historically interrupted post. Moved, not fatal.
        self._build_v0_db([
            ("INSERT INTO completed_vouchers (bill_no, date, amount, balance, beat, salesman)"
             " VALUES (?, ?, ?, ?, ?, ?)",
             ("B001", "2026-01-01", "100.00", "0.00", "beat1", "sm1")),
            (self._INSTALLMENT, ("B001", "2026-01-02", "100.00", "sm1")),
        ])
        coll_store.init_db()
        self.assertEqual(self._user_version(), 1)
        self.assertEqual(self._query("SELECT * FROM installments"), [])
        moved = self._query("SELECT bill_no, amount FROM completed_installments")
        self.assertEqual(moved, [{"bill_no": "B001", "amount": "100.00"}])

    def test_true_orphan_installment_blocks_migration(self):
        self._build_v0_db([(self._INSTALLMENT, ("B404", "2026-01-01", "10.00", "sm1"))])
        with self.assertRaises(coll_store.MigrationError) as ctx:
            coll_store.init_db()
        self.assertIn("no matching voucher", str(ctx.exception))

    def test_fresh_db_stamped_v1_and_idempotent(self):
        coll_store.init_db()
        self.assertEqual(self._user_version(), 1)
        coll_store.init_db()  # second run is a no-op, not an error
        self.assertEqual(self._user_version(), 1)


# ---------------------------------------------------------------------------
# build_print_collection_html / write_print_collection_html
# ---------------------------------------------------------------------------

class TestBuildPrintCollectionHtml(unittest.TestCase):
    def _report(self, beat="beat1", salesman="sm1", bills=("101",)):
        return {
            "selection_type": "beat_salesman",
            "selection": [beat, salesman],
            "date": "2026-01-01",
            "stages": {"start": "confirmed", "submit": "", "post": ""},
            "vouchers": [
                {"bill_no": b, "date": "2026-01-01", "balance": "50.00",
                 "payment": "", "payment_date": "", "beat": beat, "salesman": salesman}
                for b in bills
            ],
        }

    def test_contains_header_and_each_report_heading(self):
        html = coll_store.build_print_collection_html(
            [self._report("beat1", "sm1"), self._report("beat2", "sm2")])
        self.assertIn("COLLECTION LIST", html)
        self.assertIn("beat1 / sm1", html)
        self.assertIn("beat2 / sm2", html)

    def test_auto_print_flag_controls_script(self):
        reports = [self._report()]
        self.assertIn("window.print()",
                      coll_store.build_print_collection_html(reports, auto_print=True))
        self.assertNotIn("window.print()",
                         coll_store.build_print_collection_html(reports))

    def test_empty_reports_return_empty_string(self):
        self.assertEqual(coll_store.build_print_collection_html([]), "")

    def test_write_variant_writes_doc_without_auto_print(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "print_test.html"
            coll_store.write_print_collection_html(out, [self._report()])
            html = out.read_text(encoding="utf-8")
            self.assertIn("COLLECTION LIST", html)
            self.assertNotIn("window.print()", html)


# ---------------------------------------------------------------------------
# coll_print permission backfill in init_db
# ---------------------------------------------------------------------------

class TestCollPrintPermissionBackfill(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        (self.tmp / "data").mkdir()
        self._patch = patch.object(coll_store, "DATA_DIR", self.tmp / "data")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmpdir.cleanup()

    def _perm_rows(self):
        conn = coll_store.get_db()
        try:
            return {(r["role"], r["action_key"])
                    for r in conn.execute("SELECT role, action_key FROM permissions")}
        finally:
            conn.close()

    def _exec(self, sql, params=()):
        conn = coll_store.get_db()
        try:
            with conn:
                conn.execute(sql, params)
        finally:
            conn.close()

    def test_fresh_db_gets_coll_print(self):
        coll_store.init_db()
        rows = self._perm_rows()
        self.assertIn(("supervisor", "coll_print"), rows)
        self.assertIn(("distributor", "coll_print"), rows)
        self.assertNotIn(("salesman", "coll_print"), rows)

    def test_existing_seeded_db_gains_coll_print(self):
        # Simulate an install seeded before the key existed.
        coll_store.init_db()
        self._exec("DELETE FROM permissions")
        self._exec("INSERT INTO permissions (role, action_key) VALUES (?, ?)",
                   ("salesman", "reports"))
        coll_store.init_db()
        rows = self._perm_rows()
        self.assertIn(("supervisor", "coll_print"), rows)
        self.assertIn(("distributor", "coll_print"), rows)
        self.assertIn(("salesman", "reports"), rows)

    def test_csv_seed_not_skipped_on_fresh_db(self):
        # The coll_print backfill must run after the CSV seed; if it ran first,
        # the non-empty table would make the seed a no-op on fresh installs.
        csv_path = self.tmp / "data" / "permissions.csv"
        csv_path.write_text("role,action_key\nsalesman,reports\nsupervisor,reports\n",
                            encoding="utf-8")
        coll_store.init_db()
        rows = self._perm_rows()
        self.assertIn(("salesman", "reports"), rows)
        self.assertIn(("supervisor", "reports"), rows)
        self.assertIn(("supervisor", "coll_print"), rows)
        self.assertIn(("distributor", "coll_print"), rows)


if __name__ == "__main__":
    unittest.main()
