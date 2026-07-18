"""
Unit tests for scripts/coll_api.py — RBAC ownership (IDOR) checks.

Runs the real FastAPI app on a live uvicorn server (background thread, same
process) against an isolated temp DB/staging dir, so requests exercise the
full route + session + permission stack exactly as a browser would. Uses
stdlib urllib + http.cookiejar for HTTP calls — no new test dependencies
(fastapi/uvicorn are already required by requirements.txt).

Run:  python -m unittest discover -s tests -v
"""

import json
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import coll_api
import coll_data
import coll_orchestrate
import coll_store


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ApiTestCase(unittest.TestCase):
    """Base: isolated temp data/staging/archive dirs + a live coll_api server."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        (self.tmp / "data").mkdir()
        (self.tmp / "staging").mkdir()
        (self.tmp / "archive").mkdir()
        (self.tmp / "prints").mkdir()

        # Path constants are imported by value into each module — each needs
        # its own patch (same pattern as test_coll_orchestrate.OrchestrateTestCase).
        self._patches = [
            patch.object(coll_store, "DATA_DIR", self.tmp / "data"),
            patch.object(coll_store, "STAGING_DIR", self.tmp / "staging"),
            patch.object(coll_store, "ARCHIVE_DIR", self.tmp / "archive"),
            patch.object(coll_data, "DATA_DIR", self.tmp / "data"),
            patch.object(coll_data, "STAGING_DIR", self.tmp / "staging"),
            patch.object(coll_orchestrate, "STAGING_DIR", self.tmp / "staging"),
            patch.object(coll_api, "STAGING_DIR", self.tmp / "staging"),
        ]
        for p in self._patches:
            p.start()

        coll_store.ensure_db()
        self._seed_permissions()
        coll_api._sessions.clear()

        self.port = _free_port()
        config = uvicorn.Config(coll_api.app, host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(500):
            if self.server.started:
                break
            time.sleep(0.01)
        else:
            self.fail("test server did not start in time")

        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()

    # ------------------------------------------------------------------
    # HTTP helpers — one CookieJar per opener == one browser session
    # ------------------------------------------------------------------

    def _client(self):
        jar = CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def _post(self, opener, path, data):
        body = urlencode(data).encode()
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        try:
            resp = opener.open(req, timeout=5)
            return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def _get(self, opener, path):
        try:
            resp = opener.open(self.base + path, timeout=5)
            return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def _login(self, name, password):
        opener = self._client()
        self._post(opener, "/login", {"username": name, "password": password})
        return opener

    # ------------------------------------------------------------------
    # Seed helpers
    # ------------------------------------------------------------------

    def _seed_permissions(self):
        """Load the real data/permissions.csv into the temp DB.

        Not hardcoded here so the RBAC tests stay in sync with the actual
        permission grants instead of a second, driftable copy of them.
        """
        import csv
        real_csv = Path(__file__).resolve().parent.parent / "data" / "permissions.csv"
        conn = coll_store.get_db()
        try:
            with real_csv.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    # OR IGNORE: init_db's coll_print backfill pre-inserts two
                    # of these rows, and (role, action_key) is the primary key.
                    conn.execute(
                        "INSERT OR IGNORE INTO permissions (role, action_key) VALUES (?, ?)",
                        (row["role"], row["action_key"]),
                    )
            conn.commit()
        finally:
            conn.close()

    def _add_user(self, name, role, password):
        conn = coll_store.get_db()
        try:
            conn.execute(
                "INSERT INTO users (name, role, password_hash) VALUES (?, ?, ?)",
                (name, role, coll_store.hash_password(password)),
            )
            conn.commit()
        finally:
            conn.close()

    def _add_beat(self, name, salesman):
        conn = coll_store.get_db()
        try:
            conn.execute("INSERT INTO beats (name, salesman) VALUES (?, ?)", (name, salesman))
            conn.commit()
        finally:
            conn.close()

    def _add_voucher(self, bill_no, beat, salesman, balance="100.00"):
        conn = coll_store.get_db()
        try:
            conn.execute(
                "INSERT INTO vouchers "
                "(bill_no, date, amount, balance, beat, salesman, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (bill_no, "2026-01-01", balance, balance, beat, salesman,
                 "test", "2026-01-01T00:00:00"),
            )
            conn.commit()
        finally:
            conn.close()

    def _write_staging_report(self, stem, beat, salesman, start="confirmed", submit="", vouchers=None):
        data = {
            "selection_type": "beat_salesman",
            "selection": [beat, salesman],
            "date": "2026-01-01",
            "stages": {"start": start, "submit": submit, "post": ""},
            "vouchers": vouchers if vouchers is not None else [
                {"bill_no": "900", "date": "2026-01-01", "balance": "50.00",
                 "payment": "", "payment_date": "", "beat": beat, "salesman": salesman},
            ],
        }
        path = self.tmp / "staging" / f"{stem}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# Gap 1 — POST /coll/start/generate must not trust client-supplied beat/salesman
# ---------------------------------------------------------------------------

class TestCollStartGenerate(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("smA", "salesman", "pwA")
        self._add_user("smB", "salesman", "pwB")
        self._add_user("dist", "distributor", "pwD")
        self._add_beat("beatA", "smA")
        self._add_beat("beatB", "smB")
        self._add_voucher("100", "beatA", "smA")
        self._add_voucher("200", "beatB", "smB")

    def test_salesman_cannot_impersonate_another_salesman(self):
        opener = self._login("smA", "pwA")
        status, body = self._post(opener, "/coll/start/generate",
                                  {"beat": "beatA", "salesman": "smB"})
        self.assertEqual(status, 200)
        self.assertIn("You can only generate a collection list for yourself.", body)
        self.assertEqual(list((self.tmp / "staging").glob("coll*.json")), [])

    def test_salesman_cannot_generate_for_unassigned_beat(self):
        opener = self._login("smA", "pwA")
        status, body = self._post(opener, "/coll/start/generate",
                                  {"beat": "beatB", "salesman": "smA"})
        self.assertEqual(status, 200)
        self.assertIn("You are not assigned to that beat.", body)
        self.assertEqual(list((self.tmp / "staging").glob("coll*.json")), [])

    def test_salesman_can_generate_for_own_beat(self):
        opener = self._login("smA", "pwA")
        status, body = self._post(opener, "/coll/start/generate",
                                  {"beat": "beatA", "salesman": "smA"})
        self.assertEqual(status, 200)
        self.assertIn('name="report_stem"', body)
        self.assertEqual(len(list((self.tmp / "staging").glob("coll*.json"))), 1)

    def test_distributor_is_unrestricted(self):
        opener = self._login("dist", "pwD")
        status, body = self._post(opener, "/coll/start/generate",
                                  {"beat": "beatB", "salesman": "smB"})
        self.assertEqual(status, 200)
        self.assertIn('name="report_stem"', body)


# ---------------------------------------------------------------------------
# POST /coll/start/beat — the salesman-picker step ("Step 2 of 2") should be
# skipped whenever there's only one possible salesman for the beat, which is
# always true for a salesman generating their own list now that RBAC
# restricts them to assigned beats. Supervisor/distributor still see the
# picker when a beat's pending vouchers span more than one salesman.
# ---------------------------------------------------------------------------

class TestCollStartPickBeatSkipsStep2(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("smA", "salesman", "pwA")
        self._add_user("smB", "salesman", "pwB")
        self._add_user("dist", "distributor", "pwD")
        self._add_beat("beatA", "smA")
        self._add_beat("beatMixed", "smA")
        self._add_voucher("100", "beatA", "smA")
        # Historical vouchers under two different salesmen for the same beat.
        self._add_voucher("300", "beatMixed", "smA")
        self._add_voucher("400", "beatMixed", "smB")

    def test_salesman_single_salesman_beat_skips_picker_straight_to_preview(self):
        opener = self._login("smA", "pwA")
        status, body = self._post(opener, "/coll/start/beat", {"beat": "beatA"})
        self.assertEqual(status, 200)
        self.assertIn('name="report_stem"', body)
        self.assertNotIn("Select salesman for", body)
        self.assertEqual(len(list((self.tmp / "staging").glob("coll*.json"))), 1)

    def test_salesman_cannot_probe_unassigned_beat(self):
        self._add_beat("beatForeign", "smB")
        self._add_voucher("500", "beatForeign", "smB")
        opener = self._login("smA", "pwA")
        status, body = self._post(opener, "/coll/start/beat", {"beat": "beatForeign"})
        self.assertEqual(status, 200)
        self.assertIn("You are not assigned to that beat.", body)
        self.assertEqual(list((self.tmp / "staging").glob("coll*.json")), [])

    def test_distributor_sees_picker_when_beat_has_multiple_salesmen(self):
        opener = self._login("dist", "pwD")
        status, body = self._post(opener, "/coll/start/beat", {"beat": "beatMixed"})
        self.assertEqual(status, 200)
        self.assertIn("Select salesman for", body)
        self.assertIn("smA", body)
        self.assertIn("smB", body)
        self.assertEqual(list((self.tmp / "staging").glob("coll*.json")), [])

    def test_distributor_single_salesman_beat_also_skips_picker(self):
        opener = self._login("dist", "pwD")
        status, body = self._post(opener, "/coll/start/beat", {"beat": "beatA"})
        self.assertEqual(status, 200)
        self.assertIn('name="report_stem"', body)
        self.assertNotIn("Select salesman for", body)


# ---------------------------------------------------------------------------
# GET /coll/start — beats already locked by an in-flight report must be
# disabled in the dropdown and sorted to the bottom of the list, since
# selecting one can never succeed (only one active report per beat allowed).
# ---------------------------------------------------------------------------

class TestCollStartBeatDropdownOrdering(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("smA", "salesman", "pwA")
        self._add_beat("beatA", "smA")
        self._add_beat("beatB", "smA")
        self._add_beat("beatC", "smA")
        self._add_voucher("100", "beatA", "smA")
        self._add_voucher("200", "beatB", "smA")
        self._add_voucher("300", "beatC", "smA")
        # beatB has an active (awaiting-approval) staging report.
        self._write_staging_report("coll1-beat_salesman-beatB_smA", "beatB", "smA", start="new")

    def test_active_beat_disabled_and_sorted_to_bottom(self):
        opener = self._login("smA", "pwA")
        status, body = self._get(opener, "/coll/start")
        self.assertEqual(status, 200)

        self.assertIn('value="beatB" disabled', body)
        self.assertNotIn('value="beatA" disabled', body)
        self.assertNotIn('value="beatC" disabled', body)

        # Both available beats must be listed before the locked one.
        pos_a = body.index('value="beatA"')
        pos_b = body.index('value="beatB"')
        pos_c = body.index('value="beatC"')
        self.assertLess(pos_a, pos_b)
        self.assertLess(pos_c, pos_b)


# ---------------------------------------------------------------------------
# Gap 2 — GET/POST /coll/submit/{stem} must not expose another salesman's report
# ---------------------------------------------------------------------------

class TestCollSubmitOwnership(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("smA", "salesman", "pwA")
        self._add_user("smB", "salesman", "pwB")
        self._add_user("dist", "distributor", "pwD")
        self.stem_a = "coll20260101-beat_salesman-beatA_smA"
        self.stem_b = "coll20260101-beat_salesman-beatB_smB"
        self._write_staging_report(self.stem_a, "beatA", "smA")
        self._write_staging_report(self.stem_b, "beatB", "smB")

    def test_owner_can_view_own_report(self):
        opener = self._login("smA", "pwA")
        status, body = self._get(opener, f"/coll/submit/{self.stem_a}")
        self.assertEqual(status, 200)
        self.assertNotIn("Report not found.", body)

    def test_salesman_cannot_view_other_salesman_report(self):
        opener = self._login("smA", "pwA")
        status, body = self._get(opener, f"/coll/submit/{self.stem_b}")
        self.assertEqual(status, 200)
        self.assertIn("Report not found.", body)

    def test_salesman_cannot_submit_payment_on_other_salesman_report(self):
        opener = self._login("smA", "pwA")
        status, body = self._post(opener, f"/coll/submit/{self.stem_b}",
                                  {"action": "save", "pay_900": "50.00"})
        self.assertEqual(status, 200)
        self.assertIn("Report not found.", body)

        # Confirm nothing was written to B's report.
        data = json.loads((self.tmp / "staging" / f"{self.stem_b}.json").read_text())
        self.assertEqual(data["vouchers"][0]["payment"], "")

    def test_owner_can_submit_own_payment(self):
        opener = self._login("smB", "pwB")
        status, body = self._post(opener, f"/coll/submit/{self.stem_b}",
                                  {"action": "save", "pay_900": "50.00"})
        self.assertEqual(status, 200)
        self.assertNotIn("Report not found.", body)

        data = json.loads((self.tmp / "staging" / f"{self.stem_b}.json").read_text())
        self.assertEqual(data["vouchers"][0]["payment"], "50.00")

    def test_distributor_can_view_any_report(self):
        opener = self._login("dist", "pwD")
        status, body = self._get(opener, f"/coll/submit/{self.stem_a}")
        self.assertEqual(status, 200)
        self.assertNotIn("Report not found.", body)


# ---------------------------------------------------------------------------
# GET /coll/submit/{stem} must show the running total and count of payments
# already recorded (server-rendered on load; live updates as the salesman
# types are client-side JS, not exercised by this stdlib HTTP test).
# ---------------------------------------------------------------------------

class TestCollSubmitPaymentSummary(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("smA", "salesman", "pwA")
        self.stem = "coll20260101-beat_salesman-beatA_smA"
        self._write_staging_report(
            self.stem, "beatA", "smA",
            vouchers=[
                {"bill_no": "100", "date": "2026-01-01", "balance": "50.00",
                 "payment": "20.00", "payment_date": "2026-01-01", "beat": "beatA", "salesman": "smA"},
                {"bill_no": "200", "date": "2026-01-01", "balance": "75.00",
                 "payment": "75.00", "payment_date": "2026-01-01", "beat": "beatA", "salesman": "smA"},
                {"bill_no": "300", "date": "2026-01-01", "balance": "30.00",
                 "payment": "", "payment_date": "", "beat": "beatA", "salesman": "smA"},
            ],
        )

    def test_summary_reflects_existing_payments(self):
        opener = self._login("smA", "pwA")
        status, body = self._get(opener, f"/coll/submit/{self.stem}")
        self.assertEqual(status, 200)
        self.assertIn('id="payment-count"', body)
        self.assertIn('id="payment-total"', body)
        self.assertIn("2 vouchers collected", body)
        self.assertIn('id="payment-total">95.00', body)


# ---------------------------------------------------------------------------
# Gap 3 — POST /coll/start/confirm (action=cancel) must not cancel another
# salesman's collection list
# ---------------------------------------------------------------------------

class TestCollStartCancelOwnership(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("smA", "salesman", "pwA")
        self._add_user("smB", "salesman", "pwB")
        self._add_user("dist", "distributor", "pwD")
        self.stem_a = "coll20260101-beat_salesman-beatA_smA"
        self._write_staging_report(self.stem_a, "beatA", "smA", start="new")

    def test_other_salesman_cannot_cancel(self):
        opener = self._login("smB", "pwB")
        status, body = self._post(opener, "/coll/start/confirm",
                                  {"action": "cancel", "report_stem": self.stem_a, "beat": "beatA"})
        self.assertEqual(status, 200)
        self.assertIn("Report not found.", body)
        self.assertTrue((self.tmp / "staging" / f"{self.stem_a}.json").exists())

    def test_owner_can_cancel_own_report(self):
        opener = self._login("smA", "pwA")
        status, body = self._post(opener, "/coll/start/confirm",
                                  {"action": "cancel", "report_stem": self.stem_a, "beat": "beatA"})
        self.assertEqual(status, 200)
        self.assertIn("Collection list cancelled.", body)
        self.assertFalse((self.tmp / "staging" / f"{self.stem_a}.json").exists())

    def test_distributor_can_cancel_any_report(self):
        opener = self._login("dist", "pwD")
        status, body = self._post(opener, "/coll/start/confirm",
                                  {"action": "cancel", "report_stem": self.stem_a, "beat": "beatA"})
        self.assertEqual(status, 200)
        self.assertIn("Collection list cancelled.", body)
        self.assertFalse((self.tmp / "staging" / f"{self.stem_a}.json").exists())


# ---------------------------------------------------------------------------
# GET /reports/beat[/{name}] and /reports/salesman/{name} must be scoped to
# the logged-in salesman's own beats/name, not every beat/salesman in the DB.
# ---------------------------------------------------------------------------

class TestReportsScopedToSalesman(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("smA", "salesman", "pwA")
        self._add_user("smB", "salesman", "pwB")
        self._add_user("dist", "distributor", "pwD")
        self._add_beat("beatA", "smA")
        self._add_beat("beatB", "smB")
        self._add_voucher("100", "beatA", "smA")
        self._add_voucher("200", "beatB", "smB")

    def test_beat_picker_only_lists_own_beats(self):
        opener = self._login("smA", "pwA")
        status, body = self._get(opener, "/reports/beat")
        self.assertEqual(status, 200)
        self.assertIn('href="/reports/beat/beatA"', body)
        self.assertNotIn('href="/reports/beat/beatB"', body)

    def test_beat_picker_unrestricted_for_distributor(self):
        opener = self._login("dist", "pwD")
        status, body = self._get(opener, "/reports/beat")
        self.assertEqual(status, 200)
        self.assertIn('href="/reports/beat/beatA"', body)
        self.assertIn('href="/reports/beat/beatB"', body)

    def test_salesman_can_view_own_beat_detail(self):
        opener = self._login("smA", "pwA")
        status, body = self._get(opener, "/reports/beat/beatA")
        self.assertEqual(status, 200)
        self.assertNotIn("You are not assigned to that beat.", body)

    def test_salesman_cannot_view_other_beat_detail(self):
        opener = self._login("smA", "pwA")
        status, body = self._get(opener, "/reports/beat/beatB")
        self.assertEqual(status, 200)
        self.assertIn("You are not assigned to that beat.", body)

    def test_distributor_can_view_any_beat_detail(self):
        opener = self._login("dist", "pwD")
        status, body = self._get(opener, "/reports/beat/beatB")
        self.assertEqual(status, 200)
        self.assertNotIn("You are not assigned to that beat.", body)

    def test_salesman_can_view_own_salesman_detail(self):
        opener = self._login("smA", "pwA")
        status, body = self._get(opener, "/reports/salesman/smA")
        self.assertEqual(status, 200)
        self.assertNotIn("You can only view your own pending collections.", body)

    def test_salesman_cannot_view_other_salesman_detail(self):
        opener = self._login("smA", "pwA")
        status, body = self._get(opener, "/reports/salesman/smB")
        self.assertEqual(status, 200)
        self.assertIn("You can only view your own pending collections.", body)

    def test_distributor_can_view_any_salesman_detail(self):
        opener = self._login("dist", "pwD")
        status, body = self._get(opener, "/reports/salesman/smB")
        self.assertEqual(status, 200)
        self.assertNotIn("You can only view your own pending collections.", body)


# ---------------------------------------------------------------------------
# Report stems must be confined to STAGING_DIR — no path traversal via the
# {stem} path parameter or the report_stem form field.
# ---------------------------------------------------------------------------

class TestStemPathTraversal(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("smA", "salesman", "pwA")
        self._add_user("sup", "supervisor", "pwS")
        # A well-formed report sitting OUTSIDE staging: even a target the
        # handler would otherwise accept must be rejected purely on the stem.
        self.secret = self.tmp / "data" / "secret.json"
        self.secret.write_text(json.dumps({
            "selection_type": "beat_salesman",
            "selection": ["beatA", "smA"],
            "stages": {"start": "new", "submit": "", "post": ""},
            "vouchers": [],
        }), encoding="utf-8")

    def test_form_stem_with_separators_cannot_delete_outside_staging(self):
        opener = self._login("smA", "pwA")
        for stem in ("../data/secret", "..\\data\\secret", "../../etc/passwd"):
            status, body = self._post(opener, "/coll/start/confirm",
                                      {"action": "cancel", "report_stem": stem})
            self.assertEqual(status, 200)
            self.assertIn("Report not found.", body)
        self.assertTrue(self.secret.exists())

    def test_path_param_with_encoded_backslash_is_rejected(self):
        opener = self._login("sup", "pwS")
        status, body = self._get(opener, "/coll/approve-start/..%5Cdata%5Csecret")
        self.assertEqual(status, 200)
        self.assertIn("Report not found.", body)


# ---------------------------------------------------------------------------
# Stage guards on the POST transition endpoints — a request that arrives for
# a report in the wrong stage (stale page, forged URL) must be rejected.
# ---------------------------------------------------------------------------

class TestStageGuardEndpoints(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("smA", "salesman", "pwA")
        self._add_user("sup", "supervisor", "pwS")
        self._add_user("dist", "distributor", "pwD")
        self._add_beat("beatA", "smA")
        self._add_voucher("900", "beatA", "smA", balance="50.00")
        self.stem = "coll20260101-beat_salesman-beatA_smA"

    def _report_with_payment(self, submit, payment="20.00"):
        return self._write_staging_report(
            self.stem, "beatA", "smA", start="confirmed", submit=submit,
            vouchers=[{"bill_no": "900", "date": "2026-01-01", "balance": "50.00",
                       "payment": payment, "payment_date": "2026-01-01",
                       "beat": "beatA", "salesman": "smA"}],
        )

    def _voucher_balance(self):
        conn = coll_store.get_db()
        try:
            row = conn.execute("SELECT balance FROM vouchers WHERE bill_no = '900'").fetchone()
            return row["balance"] if row else None
        finally:
            conn.close()

    def test_unapproved_report_cannot_be_posted(self):
        self._report_with_payment(submit="submitted")
        opener = self._login("dist", "pwD")
        status, body = self._post(opener, f"/coll/post/{self.stem}", {"action": "post"})
        self.assertEqual(status, 200)
        self.assertIn("not approved for posting", body)
        self.assertEqual(self._voucher_balance(), "50.00")

    def test_second_post_of_same_report_is_rejected(self):
        path = self._report_with_payment(submit="confirmed")
        opener = self._login("dist", "pwD")

        # Another session holds the posting claim -> fail fast, no deduction.
        self.assertTrue(coll_store.acquire_post_claim(path))
        status, body = self._post(opener, f"/coll/post/{self.stem}", {"action": "post"})
        self.assertIn("already being posted", body)
        self.assertEqual(self._voucher_balance(), "50.00")
        coll_store.release_post_claim(path)

        # Normal post succeeds exactly once...
        status, body = self._post(opener, f"/coll/post/{self.stem}", {"action": "post"})
        self.assertIn("Posted.", body)
        self.assertEqual(self._voucher_balance(), "30.00")

        # ...and a repeat click finds the report archived, not re-postable.
        status, body = self._post(opener, f"/coll/post/{self.stem}", {"action": "post"})
        self.assertIn("Report not found.", body)
        self.assertEqual(self._voucher_balance(), "30.00")

    def test_salesman_cannot_edit_payments_after_submitting(self):
        path = self._report_with_payment(submit="submitted")
        opener = self._login("smA", "pwA")
        status, body = self._post(opener, f"/coll/submit/{self.stem}",
                                  {"action": "save", "pay_900": "1.00"})
        self.assertEqual(status, 200)
        self.assertIn("cannot be edited", body)
        sidecar = path.parent / f"{path.stem}-installments.json"
        self.assertFalse(sidecar.exists())

    def test_supervisor_cannot_cancel_report_in_submit_pipeline(self):
        path = self._report_with_payment(submit="submitted")
        opener = self._login("sup", "pwS")
        status, body = self._post(opener, f"/coll/approve-start/{self.stem}",
                                  {"action": "cancel"})
        self.assertEqual(status, 200)
        self.assertIn("cannot be cancelled", body)
        self.assertTrue(path.exists())

    def test_approve_submit_requires_submitted_stage(self):
        self._report_with_payment(submit="confirmed")
        opener = self._login("sup", "pwS")
        status, body = self._post(opener, f"/coll/approve-submit/{self.stem}",
                                  {"action": "approve"})
        self.assertEqual(status, 200)
        self.assertIn("cannot be approved", body)

    def test_supervisor_cannot_approve_payment_over_balance(self):
        # Staged data from an unvalidated client stops at the approval gate.
        path = self._report_with_payment(submit="submitted", payment="50.01")
        opener = self._login("sup", "pwS")
        status, body = self._post(opener, f"/coll/approve-submit/{self.stem}",
                                  {"action": "approve"})
        self.assertEqual(status, 200)
        self.assertIn("exceeds balance", body)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["stages"]["submit"], "submitted")

    def test_tampered_staged_balance_cannot_overcollect(self):
        # A hand-edited staging file inflates the balance copy to 500 so a
        # 60.00 payment looks legitimate — master says the voucher is 50.00.
        # Both the approval gate and the post backstop must refuse.
        def tampered(submit):
            return self._write_staging_report(
                self.stem, "beatA", "smA", start="confirmed", submit=submit,
                vouchers=[{"bill_no": "900", "date": "2026-01-01", "balance": "500.00",
                           "payment": "60.00", "payment_date": "2026-01-01",
                           "beat": "beatA", "salesman": "smA"}],
            )

        tampered(submit="submitted")
        opener = self._login("sup", "pwS")
        status, body = self._post(opener, f"/coll/approve-submit/{self.stem}",
                                  {"action": "approve"})
        self.assertEqual(status, 200)
        self.assertIn("exceeds balance (50.00)", body)

        tampered(submit="confirmed")
        opener = self._login("dist", "pwD")
        status, body = self._post(opener, f"/coll/post/{self.stem}", {"action": "post"})
        self.assertEqual(status, 200)
        self.assertIn("exceeds balance (50.00)", body)
        self.assertEqual(self._voucher_balance(), "50.00")

    def test_overpaid_confirmed_report_cannot_be_posted(self):
        # Final backstop at post, even if approval was somehow bypassed.
        self._report_with_payment(submit="confirmed", payment="50.01")
        opener = self._login("dist", "pwD")
        status, body = self._post(opener, f"/coll/post/{self.stem}", {"action": "post"})
        self.assertEqual(status, 200)
        self.assertIn("exceeds balance", body)
        self.assertEqual(self._voucher_balance(), "50.00")

    def test_post_records_logged_in_user_as_created_by(self):
        self._report_with_payment(submit="confirmed", payment="20.00")
        opener = self._login("dist", "pwD")
        status, body = self._post(opener, f"/coll/post/{self.stem}", {"action": "post"})
        self.assertIn("Posted.", body)
        conn = coll_store.get_db()
        try:
            row = conn.execute(
                "SELECT created_by FROM installments WHERE bill_no = '900'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["created_by"], "dist")


# ---------------------------------------------------------------------------
# POST /coll/submit/{stem} must validate payments server-side — the
# type="number" input is advisory, not a boundary.
# ---------------------------------------------------------------------------

class TestSubmitPaymentValidation(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("smA", "salesman", "pwA")
        self.stem = "coll20260101-beat_salesman-beatA_smA"
        self.path = self._write_staging_report(
            self.stem, "beatA", "smA", start="confirmed", submit="",
            vouchers=[{"bill_no": "900", "date": "2026-01-01", "balance": "50.00",
                       "payment": "", "payment_date": "", "beat": "beatA", "salesman": "smA"}],
        )
        self.sidecar = self.path.parent / f"{self.path.stem}-installments.json"
        self.opener = self._login("smA", "pwA")

    def _save(self, payment):
        return self._post(self.opener, f"/coll/submit/{self.stem}",
                          {"action": "save", "pay_900": payment})

    def test_non_numeric_payment_rejected_and_nothing_saved(self):
        status, body = self._save("abc")
        self.assertEqual(status, 200)
        self.assertIn("Nothing saved", body)
        self.assertIn("not a number", body)
        # Errors are inline per-field bubbles now, not a joined top-of-page list.
        self.assertNotIn("fix these payments", body)
        self.assertIn("payment(s) need correction", body)
        self.assertIn('aria-invalid="true"', body)
        self.assertIn('value="abc"', body)  # typed value retained for correction
        self.assertFalse(self.sidecar.exists())

    def test_negative_payment_rejected(self):
        status, body = self._save("-5")
        self.assertIn("cannot be negative", body)
        self.assertFalse(self.sidecar.exists())

    def test_payment_over_balance_rejected(self):
        status, body = self._save("60")
        self.assertIn("exceeds balance", body)
        self.assertFalse(self.sidecar.exists())

    def test_two_invalid_payments_flag_both_rows(self):
        stem = "coll20260102-beat_salesman-beatB_smA"
        path = self._write_staging_report(
            stem, "beatB", "smA", start="confirmed", submit="",
            vouchers=[{"bill_no": "901", "date": "2026-01-01", "balance": "50.00",
                       "payment": "", "payment_date": "", "beat": "beatB", "salesman": "smA"},
                      {"bill_no": "902", "date": "2026-01-01", "balance": "30.00",
                       "payment": "", "payment_date": "", "beat": "beatB", "salesman": "smA"}],
        )
        status, body = self._post(self.opener, f"/coll/submit/{stem}",
                                  {"action": "save", "pay_901": "60", "pay_902": "-1"})
        self.assertEqual(status, 200)
        self.assertIn("2 payment(s) need correction", body)
        self.assertIn("exceeds balance", body)
        self.assertIn("cannot be negative", body)
        self.assertEqual(body.count('aria-invalid="true"'), 2)
        sidecar = path.parent / f"{path.stem}-installments.json"
        self.assertFalse(sidecar.exists())

    def test_mixed_valid_and_invalid_saves_nothing(self):
        stem = "coll20260103-beat_salesman-beatC_smA"
        path = self._write_staging_report(
            stem, "beatC", "smA", start="confirmed", submit="",
            vouchers=[{"bill_no": "903", "date": "2026-01-01", "balance": "50.00",
                       "payment": "", "payment_date": "", "beat": "beatC", "salesman": "smA"},
                      {"bill_no": "904", "date": "2026-01-01", "balance": "30.00",
                       "payment": "", "payment_date": "", "beat": "beatC", "salesman": "smA"}],
        )
        status, body = self._post(self.opener, f"/coll/submit/{stem}",
                                  {"action": "save", "pay_903": "20", "pay_904": "40"})
        self.assertEqual(status, 200)
        self.assertIn("1 payment(s) need correction", body)
        # Only the invalid row is flagged; the valid row keeps its (normalized) value.
        self.assertEqual(body.count('aria-invalid="true"'), 1)
        self.assertIn('value="20.00"', body)
        sidecar = path.parent / f"{path.stem}-installments.json"
        self.assertFalse(sidecar.exists())

    def test_valid_payment_saved_quantized(self):
        status, body = self._save("20")
        self.assertIn("Progress saved.", body)
        saved = json.loads(self.sidecar.read_text(encoding="utf-8"))
        self.assertEqual(saved["900"]["payment"], "20.00")


# ---------------------------------------------------------------------------
# Print Collection List — /coll/print (coll_print permission)
# ---------------------------------------------------------------------------

class TestCollPrint(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("sup", "supervisor", "pwS")
        self._add_user("dist", "distributor", "pwD")
        self._add_user("smA", "salesman", "pwA")
        self.stems = []
        for i, beat in enumerate(("beatA", "beatB", "beatC", "beatD"), start=1):
            stem = f"coll2026010{i}-beat_salesman-{beat}_smA"
            self._write_staging_report(stem, beat, "smA", start="confirmed", submit="")
            self.stems.append(stem)
        # Already submitted — must not be offered for printing.
        self._write_staging_report("coll20260109-beat_salesman-beatX_smA",
                                   "beatX", "smA", start="confirmed", submit="submitted")

    def _post_stems(self, opener, stems):
        return self._post(opener, "/coll/print", [("stems", s) for s in stems])

    def test_menu_card_shown_to_supervisor_and_distributor_only(self):
        for name, pw in (("sup", "pwS"), ("dist", "pwD")):
            status, body = self._get(self._login(name, pw), "/menu")
            self.assertIn("Print Collection List", body)
        status, body = self._get(self._login("smA", "pwA"), "/menu")
        self.assertNotIn("Print Collection List", body)

    def test_salesman_denied(self):
        opener = self._login("smA", "pwA")
        status, body = self._get(opener, "/coll/print")
        self.assertIn("have permission for this action", body)
        status, body = self._post_stems(opener, [self.stems[0]])
        self.assertIn("have permission for this action", body)

    def test_get_lists_confirmed_reports_only(self):
        opener = self._login("sup", "pwS")
        status, body = self._get(opener, "/coll/print")
        self.assertEqual(status, 200)
        for beat in ("beatA", "beatB", "beatC", "beatD"):
            self.assertIn(f"{beat} / smA", body)
        self.assertNotIn("beatX / smA", body)

    def test_get_empty_state(self):
        for p in (self.tmp / "staging").glob("coll*.json"):
            p.unlink()
        opener = self._login("dist", "pwD")
        status, body = self._get(opener, "/coll/print")
        self.assertIn("No approved collection lists to print.", body)

    def test_post_zero_selections_rejected(self):
        opener = self._login("sup", "pwS")
        status, body = self._post_stems(opener, [])
        self.assertIn("Select at least one collection list.", body)

    def test_post_more_than_three_rejected(self):
        opener = self._login("sup", "pwS")
        status, body = self._post_stems(opener, self.stems)  # 4 stems
        self.assertIn("Select at most 3 collection lists.", body)
        self.assertNotIn("COLLECTION LIST", body)

    def test_post_unknown_stem_rejected(self):
        opener = self._login("sup", "pwS")
        status, body = self._post_stems(opener, ["..%5c..%5csecrets"])
        self.assertIn("Report not found.", body)

    def test_post_submitted_stage_stem_rejected(self):
        # A crafted POST must not print reports the selection page doesn't offer.
        opener = self._login("sup", "pwS")
        status, body = self._post_stems(opener, ["coll20260109-beat_salesman-beatX_smA"])
        self.assertIn("Report not found.", body)
        self.assertNotIn("COLLECTION LIST", body)

    def test_post_up_to_three_returns_printable_document(self):
        opener = self._login("dist", "pwD")
        status, body = self._post_stems(opener, self.stems[:3])
        self.assertEqual(status, 200)
        self.assertIn("COLLECTION LIST", body)
        for beat in ("beatA", "beatB", "beatC"):
            self.assertIn(f"{beat} / smA", body)
        self.assertIn("window.print()", body)

    def test_post_single_selection_ok(self):
        opener = self._login("sup", "pwS")
        status, body = self._post_stems(opener, [self.stems[0]])
        self.assertIn("COLLECTION LIST", body)
        self.assertIn("beatA / smA", body)


# ---------------------------------------------------------------------------
# GET /voucher/{bill_no} — inline detail view behind the voucher hyperlinks
# ---------------------------------------------------------------------------

class TestVoucherDetail(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("smA", "salesman", "pwA")
        self._add_beat("beatA", "smA")
        self._add_voucher("100", "beatA", "smA", balance="60.00")
        self._add_installment("100", "2026-02-01", "40.00", "smA")

    def _add_installment(self, bill_no, date, amount, salesman, completed=False):
        table = "completed_installments" if completed else "installments"
        conn = coll_store.get_db()
        try:
            conn.execute(
                f"INSERT INTO {table} (bill_no, date, amount, salesman, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (bill_no, date, amount, salesman, "test", "2026-02-01T00:00:00"),
            )
            conn.commit()
        finally:
            conn.close()

    def _add_completed_voucher(self, bill_no, beat, salesman, amount="80.00"):
        conn = coll_store.get_db()
        try:
            conn.execute(
                "INSERT INTO completed_vouchers "
                "(bill_no, date, amount, balance, beat, salesman, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (bill_no, "2026-01-01", amount, "0.00", beat, salesman,
                 "test", "2026-01-01T00:00:00"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_full_page_shows_voucher_and_installments(self):
        opener = self._login("smA", "pwA")
        status, body = self._get(opener, "/voucher/100")
        self.assertEqual(status, 200)
        self.assertIn("Voucher Details", body)
        self.assertIn("2026-01-01", body)   # voucher date
        self.assertIn("60.00", body)        # amount/balance
        self.assertIn("2026-02-01", body)   # installment date
        self.assertIn("40.00", body)        # installment amount

    def test_fragment_returns_card_only(self):
        opener = self._login("smA", "pwA")
        status, body = self._get(opener, "/voucher/100?fragment=1")
        self.assertEqual(status, 200)
        self.assertIn("voucher-card", body)
        self.assertIn("40.00", body)
        self.assertNotIn("<header", body)   # no base.html chrome

    def test_completed_voucher_shows_badge_and_archived_installments(self):
        self._add_completed_voucher("200", "beatA", "smA")
        self._add_installment("200", "2026-03-01", "80.00", "smA", completed=True)
        opener = self._login("smA", "pwA")
        status, body = self._get(opener, "/voucher/200")
        self.assertEqual(status, 200)
        self.assertIn("Completed", body)
        self.assertIn("80.00", body)

    def test_unknown_bill_full_page(self):
        opener = self._login("smA", "pwA")
        status, body = self._get(opener, "/voucher/999")
        self.assertEqual(status, 200)
        self.assertIn("No voucher found for: 999", body)

    def test_unknown_bill_fragment_is_404_with_message(self):
        opener = self._login("smA", "pwA")
        status, body = self._get(opener, "/voucher/999?fragment=1")
        self.assertEqual(status, 404)
        self.assertIn("Voucher not found.", body)

    def test_unauthenticated_redirects_to_login(self):
        opener = self._client()  # no session cookie
        status, body = self._get(opener, "/voucher/100")
        self.assertEqual(status, 200)   # opener follows the 303 to /login
        self.assertIn('name="password"', body)
        self.assertNotIn("voucher-card", body)


# ---------------------------------------------------------------------------
# PWA static assets — manifest and service worker must reference real files
# ---------------------------------------------------------------------------

class PwaAssetTests(unittest.TestCase):
    """A manifest icon that 404s breaks Android installability silently, and a
    sw.js SHELL entry that 404s makes the whole install-event precache fail."""

    STATIC = Path(__file__).resolve().parent.parent / "static"

    def test_manifest_icons_exist_and_are_png(self):
        manifest = json.loads((self.STATIC / "manifest.json").read_text(encoding="utf-8"))
        icons = manifest.get("icons", [])
        self.assertTrue(icons, "manifest.json declares no icons")
        for icon in icons:
            self.assertTrue(icon["src"].startswith("/static/"), icon["src"])
            path = self.STATIC / icon["src"].rsplit("/", 1)[-1]
            self.assertTrue(path.is_file(), f"manifest icon missing: {icon['src']}")
            with path.open("rb") as f:
                self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n",
                                 f"{path.name} is not a PNG")

    def test_service_worker_references_exist(self):
        sw = (self.STATIC / "sw.js").read_text(encoding="utf-8")
        refs = re.findall(r"/static/([\w.\-]+)", sw)
        self.assertTrue(refs, "sw.js references no static files")
        for name in refs:
            self.assertTrue((self.STATIC / name).is_file(),
                            f"sw.js references missing file: /static/{name}")


if __name__ == "__main__":
    unittest.main()
