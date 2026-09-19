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
# Gap 1 — POST /coll/start/generate must not trust a client-supplied beat
# outside a salesman's assigned beats. Salesman selection was dropped from
# generation entirely: the form now takes only `beat`, and the resulting
# list is a beat-wide combined list (see TestCollStartGenerateCombinedList
# below for the multi-salesman behavior).
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

    def test_salesman_cannot_generate_for_unassigned_beat(self):
        opener = self._login("smA", "pwA")
        status, body = self._post(opener, "/coll/start/generate", {"beat": "beatB"})
        self.assertEqual(status, 200)
        self.assertIn("You are not assigned to that beat.", body)
        self.assertEqual(list((self.tmp / "staging").glob("coll*.json")), [])

    def test_salesman_can_generate_for_own_beat(self):
        opener = self._login("smA", "pwA")
        status, body = self._post(opener, "/coll/start/generate", {"beat": "beatA"})
        self.assertEqual(status, 200)
        self.assertIn('name="report_stem"', body)
        self.assertEqual(len(list((self.tmp / "staging").glob("coll*.json"))), 1)

    def test_distributor_is_unrestricted(self):
        opener = self._login("dist", "pwD")
        status, body = self._post(opener, "/coll/start/generate", {"beat": "beatB"})
        self.assertEqual(status, 200)
        self.assertIn('name="report_stem"', body)

    def test_no_beat_selected_shows_error(self):
        opener = self._login("dist", "pwD")
        status, body = self._post(opener, "/coll/start/generate", {"beat": ""})
        self.assertEqual(status, 200)
        self.assertIn("Select a beat.", body)


# ---------------------------------------------------------------------------
# GET /coll/start — beat-only picker (no salesman selection). Each beat
# option shows its assigned salesman for reference only, from beats.salesman.
# ---------------------------------------------------------------------------

class TestCollStartBeatForm(ApiTestCase):
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

    def test_no_salesman_field_in_form(self):
        opener = self._login("dist", "pwD")
        status, body = self._get(opener, "/coll/start")
        self.assertEqual(status, 200)
        self.assertNotIn('name="salesman"', body)

    def test_salesman_beat_list_excludes_unassigned_beats(self):
        self._add_beat("beatForeign", "smB")
        self._add_voucher("500", "beatForeign", "smB")
        opener = self._login("smA", "pwA")
        status, body = self._get(opener, "/coll/start")
        self.assertEqual(status, 200)
        self.assertNotIn("beatForeign", body)


# ---------------------------------------------------------------------------
# Beat-only generation produces a combined list spanning every salesman with
# pending vouchers on the beat, and flags (never auto-raises an Amendment
# Request for) any voucher whose own salesman differs from the beat's
# assigned salesman — a human reviews and raises one themselves if warranted.
# ---------------------------------------------------------------------------

class TestCollStartGenerateCombinedList(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("smA", "salesman", "pwA")
        self._add_user("smB", "salesman", "pwB")
        self._add_user("dist", "distributor", "pwD")
        self._add_beat("beatMixed", "smA")
        self._add_voucher("300", "beatMixed", "smA")
        self._add_voucher("400", "beatMixed", "smB")

    def test_combined_list_includes_all_salesmen_and_flags_mismatch(self):
        opener = self._login("dist", "pwD")
        status, body = self._post(opener, "/coll/start/generate", {"beat": "beatMixed"})
        self.assertEqual(status, 200)
        self.assertIn('name="report_stem"', body)
        self.assertIn("300", body)
        self.assertIn("400", body)
        self.assertIn("Different salesman", body)
        # No Amendment Request auto-raised — purely a review flag.
        self.assertEqual(coll_store.load_amendment_requests(), [])

        paths = list((self.tmp / "staging").glob("coll*.json"))
        self.assertEqual(len(paths), 1)
        data = json.loads(paths[0].read_text(encoding="utf-8"))
        self.assertEqual(data["selection_type"], "beat")
        self.assertEqual(data["selection"], ["beatMixed"])
        self.assertEqual({v["bill_no"] for v in data["vouchers"]}, {"300", "400"})

    def test_salesman_generating_own_beat_sees_combined_list(self):
        opener = self._login("smA", "pwA")
        status, body = self._post(opener, "/coll/start/generate", {"beat": "beatMixed"})
        self.assertEqual(status, 200)
        self.assertIn("400", body)
        self.assertIn("Different salesman", body)


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

    def test_fragment_returns_slim_installments(self):
        opener = self._login("smA", "pwA")
        status, body = self._get(opener, "/voucher/100?fragment=1")
        self.assertEqual(status, 200)
        self.assertIn("inline-installments", body)
        self.assertIn("2026-02-01", body)       # installment date
        self.assertIn("40.00", body)            # installment amount
        self.assertNotIn("voucher-card", body)  # slim partial, not the full card
        self.assertNotIn("<header", body)       # no base.html chrome

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


# ---------------------------------------------------------------------------
# Physical-voucher verification on Approve Collection List: the verify
# endpoint persists per-voucher/count toggles, and the approve action is
# hard-gated (web only) until verification is complete.
# ---------------------------------------------------------------------------

class TestStartVerification(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("smA", "salesman", "pwA")
        self._add_user("sup", "supervisor", "pwS")
        self._add_beat("beatA", "smA")
        self._add_voucher("900", "beatA", "smA", balance="50.00")
        self._add_voucher("901", "beatA", "smA", balance="30.00")
        self.stem = "coll20260101-beat_salesman-beatA_smA"
        self.path = self._write_staging_report(
            self.stem, "beatA", "smA", start="new", submit="",
            vouchers=[
                {"bill_no": "900", "date": "2026-01-01", "balance": "50.00",
                 "payment": "", "payment_date": "", "beat": "beatA", "salesman": "smA"},
                {"bill_no": "901", "date": "2026-01-01", "balance": "30.00",
                 "payment": "", "payment_date": "", "beat": "beatA", "salesman": "smA"},
            ])
        self.verify_url = f"/coll/approve-start/{self.stem}/verify"

    def _saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _complete_verification(self, opener):
        for bill in ("900", "901"):
            self._post(opener, self.verify_url, {"bill_no": bill, "verified": "1"})
        self._post(opener, self.verify_url, {"count": "1"})

    def test_toggle_persists_and_untoggle_removes(self):
        opener = self._login("sup", "pwS")
        status, body = self._post(opener, self.verify_url,
                                  {"bill_no": "900", "verified": "1"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(self._saved()["verification"]["bill_nos"], ["900"])
        self._post(opener, self.verify_url, {"bill_no": "900", "verified": "0"})
        self.assertEqual(self._saved()["verification"]["bill_nos"], [])

    def test_count_flag_roundtrip(self):
        opener = self._login("sup", "pwS")
        self._post(opener, self.verify_url, {"count": "1"})
        self.assertTrue(self._saved()["verification"]["count"])
        self._post(opener, self.verify_url, {"count": "0"})
        self.assertFalse(self._saved()["verification"]["count"])

    def test_review_renders_persisted_state(self):
        opener = self._login("sup", "pwS")
        self._post(opener, self.verify_url, {"bill_no": "900", "verified": "1"})
        status, body = self._get(opener, f"/coll/approve-start/{self.stem}")
        self.assertEqual(status, 200)
        self.assertIn("Verified 1 / 2", body)
        self.assertIn("checked", body)

    def test_wrong_stage_returns_409(self):
        self._write_staging_report(self.stem, "beatA", "smA",
                                   start="confirmed", submit="")
        opener = self._login("sup", "pwS")
        status, _ = self._post(opener, self.verify_url,
                               {"bill_no": "900", "verified": "1"})
        self.assertEqual(status, 409)

    def test_unknown_bill_returns_404(self):
        opener = self._login("sup", "pwS")
        status, _ = self._post(opener, self.verify_url,
                               {"bill_no": "999", "verified": "1"})
        self.assertEqual(status, 404)

    def test_param_misuse_returns_400(self):
        opener = self._login("sup", "pwS")
        status, _ = self._post(opener, self.verify_url, {})
        self.assertEqual(status, 400)
        status, _ = self._post(opener, self.verify_url,
                               {"bill_no": "900", "verified": "1", "count": "1"})
        self.assertEqual(status, 400)

    def test_salesman_gets_403(self):
        opener = self._login("smA", "pwA")
        status, _ = self._post(opener, self.verify_url,
                               {"bill_no": "900", "verified": "1"})
        self.assertEqual(status, 403)
        self.assertNotIn("verification", self._saved())

    def test_logged_out_gets_401(self):
        opener = self._client()
        status, _ = self._post(opener, self.verify_url,
                               {"bill_no": "900", "verified": "1"})
        self.assertEqual(status, 401)

    def test_approve_blocked_until_verification_complete(self):
        opener = self._login("sup", "pwS")
        status, body = self._post(opener, f"/coll/approve-start/{self.stem}",
                                  {"action": "approve"})
        self.assertEqual(status, 200)
        self.assertIn("Cannot approve", body)
        self.assertEqual(self._saved()["stages"]["start"], "new")

        # All vouchers ticked but not the count box -> still blocked.
        for bill in ("900", "901"):
            self._post(opener, self.verify_url, {"bill_no": bill, "verified": "1"})
        status, body = self._post(opener, f"/coll/approve-start/{self.stem}",
                                  {"action": "approve"})
        self.assertIn("Cannot approve", body)
        self.assertEqual(self._saved()["stages"]["start"], "new")

    def test_complete_verification_approves_and_pops_key(self):
        opener = self._login("sup", "pwS")
        self._complete_verification(opener)
        status, body = self._post(opener, f"/coll/approve-start/{self.stem}",
                                  {"action": "approve"})
        self.assertEqual(status, 200)
        self.assertIn("Collection list approved.", body)
        saved = self._saved()
        self.assertEqual(saved["stages"]["start"], "confirmed")
        self.assertNotIn("verification", saved)

    def test_return_and_cancel_need_no_verification(self):
        opener = self._login("sup", "pwS")
        status, body = self._post(opener, f"/coll/approve-start/{self.stem}",
                                  {"action": "return"})
        self.assertEqual(status, 200)
        self.assertIn("returned", body)
        self.assertFalse(self.path.exists())


# ---------------------------------------------------------------------------
# Correction requests — raise, queue visibility, apply/reject RBAC, and the
# approval gates they hold.
# ---------------------------------------------------------------------------

class TestCorrections(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("smA", "salesman", "pwA")
        self._add_user("sup", "supervisor", "pwS")
        self._add_user("dist", "distributor", "pwD")
        self._add_beat("beatA", "smA")
        self._add_voucher("900", "beatA", "smA", balance="50.00")  # amount == 50.00
        self.stem = "coll20260101-beat_salesman-beatA_smA"
        self.report_path = self._write_staging_report(
            self.stem, "beatA", "smA", start="new", submit="",
            vouchers=[{"bill_no": "900", "date": "2026-01-01", "balance": "50.00",
                       "payment": "", "payment_date": "", "beat": "beatA",
                       "salesman": "smA"}])
        self.from_path = f"/coll/approve-start/{self.stem}"

    def _add_installment(self, bill_no, date="2026-01-01", amount="10.00"):
        conn = coll_store.get_db()
        try:
            cur = conn.execute(
                "INSERT INTO installments (bill_no, date, amount, salesman, created_by, created_at)"
                " VALUES (?, ?, ?, 'smA', 'test', 't')", (bill_no, date, amount))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def _raise_voucher_amount(self, opener, new_amount="60.00"):
        return self._post(opener, "/coll/correct/900",
                          {"action": "raise", "kind": "voucher_amount",
                           "new_amount": new_amount, "note": "physical shows more",
                           "from": self.from_path})

    def _corrections(self):
        return coll_store.load_corrections()

    def test_raise_voucher_amount_records_context(self):
        opener = self._login("sup", "pwS")
        status, body = self._get(opener, f"/coll/correct/900?from={self.from_path}")
        self.assertEqual(status, 200)
        self.assertIn("Raise Correction", body)
        status, _ = self._raise_voucher_amount(opener)
        self.assertEqual(status, 200)  # 303 followed back to the review page
        corr = self._corrections()[0]
        self.assertEqual(corr["kind"], "voucher_amount")
        self.assertEqual(corr["old"], {"amount": "50.00"})
        self.assertEqual(corr["new"], {"amount": "60.00"})
        self.assertEqual(corr["report_stem"], self.stem)
        self.assertEqual(corr["origin_stage"], "start")
        self.assertEqual(corr["requested_by"], "sup")

    def test_raise_validation(self):
        opener = self._login("sup", "pwS")
        status, body = self._post(opener, "/coll/correct/900",
                                  {"action": "raise", "from": self.from_path})
        self.assertIn("Choose what kind", body)
        status, body = self._raise_voucher_amount(opener, new_amount="50.00")
        self.assertIn("same as the recorded", body)
        status, body = self._post(opener, "/coll/correct/900",
                                  {"action": "raise", "kind": "installment_amount",
                                   "new_amount": "5.00", "from": self.from_path})
        self.assertIn("Pick the installment", body)
        self.assertEqual(self._corrections(), [])

    def test_installment_amount_kind_snapshots_row(self):
        inst_id = self._add_installment("900", amount="10.00")
        opener = self._login("sup", "pwS")
        self._post(opener, "/coll/correct/900",
                   {"action": "raise", "kind": "installment_amount",
                    "installment_id": str(inst_id), "new_amount": "25.00",
                    "from": self.from_path})
        corr = self._corrections()[0]
        self.assertEqual(corr["installment_id"], inst_id)
        self.assertEqual(corr["old"], {"date": "2026-01-01", "amount": "10.00"})
        self.assertEqual(corr["new"], {"amount": "25.00"})

    def test_salesman_can_raise_and_view(self):
        # raise_correction was widened to salesman alongside Amendment
        # Requests, so a salesman can use this flow (and its "raise an
        # amendment request instead" cross-link) same as a supervisor.
        opener = self._login("smA", "pwA")
        status, body = self._get(opener, "/coll/corrections")
        self.assertEqual(status, 200)
        self.assertNotIn("permission", body)
        status, body = self._post(opener, "/coll/correct/900",
                                  {"action": "raise", "kind": "voucher_amount",
                                   "new_amount": "60.00", "from": self.from_path})
        self.assertEqual(self._corrections()[0]["requested_by"], "smA")

    def test_supervisor_views_distributor_acts(self):
        sup = self._login("sup", "pwS")
        self._raise_voucher_amount(sup)
        cid = self._corrections()[0]["id"]

        status, body = self._get(sup, "/coll/corrections")
        self.assertIn("900", body)
        self.assertIn("Pending", body)
        status, body = self._get(sup, f"/coll/corrections/{cid}")
        self.assertNotIn('value="apply"', body)  # read-only for supervisor

        status, body = self._post(sup, f"/coll/corrections/{cid}", {"action": "apply"})
        self.assertIn("permission", body)
        self.assertEqual(coll_store.load_correction(cid)["status"], "open")

        dist = self._login("dist", "pwD")
        status, body = self._get(dist, f"/coll/corrections/{cid}")
        self.assertIn('value="apply"', body)
        status, body = self._post(dist, f"/coll/corrections/{cid}", {"action": "apply"})
        self.assertIn("Correction applied", body)
        conn = coll_store.get_db()
        try:
            row = conn.execute("SELECT amount, balance FROM vouchers WHERE bill_no='900'").fetchone()
        finally:
            conn.close()
        self.assertEqual((row["amount"], row["balance"]), ("60.00", "60.00"))
        # Staged display balance refreshed too.
        staged = json.loads(self.report_path.read_text(encoding="utf-8"))
        self.assertEqual(staged["vouchers"][0]["balance"], "60.00")

    def test_open_correction_gates_verification_and_approval(self):
        sup = self._login("sup", "pwS")
        self._raise_voucher_amount(sup)
        cid = self._corrections()[0]["id"]

        # Verify endpoint refuses the disputed voucher.
        status, _ = self._post(sup, f"/coll/approve-start/{self.stem}/verify",
                               {"bill_no": "900", "verified": "1"})
        self.assertEqual(status, 409)
        # Count still saves, but approve is blocked with the corrections error.
        self._post(sup, f"/coll/approve-start/{self.stem}/verify", {"count": "1"})
        status, body = self._post(sup, f"/coll/approve-start/{self.stem}",
                                  {"action": "approve"})
        self.assertIn("correction request", body)
        saved = json.loads(self.report_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["stages"]["start"], "new")
        # Review shows the blocked state.
        status, body = self._get(sup, f"/coll/approve-start/{self.stem}")
        self.assertIn("Correction pending", body)
        self.assertIn("disabled", body)

        # Distributor applies -> voucher verifiable, approval unblocked.
        dist = self._login("dist", "pwD")
        self._post(dist, f"/coll/corrections/{cid}", {"action": "apply"})
        status, body = self._get(sup, f"/coll/approve-start/{self.stem}")
        self.assertIn("Correction applied", body)
        status, _ = self._post(sup, f"/coll/approve-start/{self.stem}/verify",
                               {"bill_no": "900", "verified": "1"})
        self.assertEqual(status, 200)
        status, body = self._post(sup, f"/coll/approve-start/{self.stem}",
                                  {"action": "approve"})
        self.assertIn("Collection list approved.", body)

    def test_reject_unblocks_without_master_change(self):
        sup = self._login("sup", "pwS")
        self._raise_voucher_amount(sup)
        cid = self._corrections()[0]["id"]
        dist = self._login("dist", "pwD")
        status, body = self._post(dist, f"/coll/corrections/{cid}",
                                  {"action": "reject",
                                   "resolution_note": "system agrees with locker copy"})
        self.assertIn("Correction rejected", body)
        conn = coll_store.get_db()
        try:
            row = conn.execute("SELECT amount FROM vouchers WHERE bill_no='900'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["amount"], "50.00")
        status, _ = self._post(sup, f"/coll/approve-start/{self.stem}/verify",
                               {"bill_no": "900", "verified": "1"})
        self.assertEqual(status, 200)

    def test_applied_entry_active_then_history_after_cancel(self):
        sup = self._login("sup", "pwS")
        self._raise_voucher_amount(sup)
        cid = self._corrections()[0]["id"]
        dist = self._login("dist", "pwD")
        self._post(dist, f"/coll/corrections/{cid}", {"action": "apply"})

        # Active while the gated report is active, linking to its review.
        status, body = self._get(dist, "/coll/corrections")
        self.assertIn("Applied", body)
        self.assertIn(f"/coll/approve-start/{self.stem}", body)

        # Cancel the report -> the applied record drops to history (derived).
        self._post(sup, f"/coll/approve-start/{self.stem}", {"action": "cancel"})
        status, body = self._get(dist, "/coll/corrections")
        self.assertIn("Recent History", body)
        self.assertNotIn("Awaiting Collection List approval", body)

    def test_withdraw_own_requests_only(self):
        sup = self._login("sup", "pwS")
        self._raise_voucher_amount(sup)
        cid = self._corrections()[0]["id"]
        dist = self._login("dist", "pwD")
        status, body = self._post(dist, "/coll/correct/900",
                                  {"action": "withdraw", "corr_id": str(cid),
                                   "from": self.from_path})
        self.assertIn("Only your own", body)
        self.assertEqual(coll_store.load_correction(cid)["status"], "open")
        status, body = self._post(sup, "/coll/correct/900",
                                  {"action": "withdraw", "corr_id": str(cid),
                                   "from": self.from_path})
        self.assertEqual(coll_store.load_correction(cid)["status"], "withdrawn")

    # -- `next` passthrough (iteration4 decision 4: lets the amend gate send
    #    the distributor back to itself once a blocking correction resolves)

    def test_next_param_round_trips_through_resolution(self):
        sup = self._login("sup", "pwS")
        self._raise_voucher_amount(sup)
        cid = self._corrections()[0]["id"]
        dist = self._login("dist", "pwD")
        status, body = self._get(dist, f"/coll/corrections/{cid}?next=/coll/amend/900")
        self.assertIn('href="/coll/amend/900"', body)
        status, body = self._post(dist, f"/coll/corrections/{cid}",
                                  {"action": "apply", "next": "/coll/amend/900"})
        self.assertIn("Correction applied", body)
        # No more Continue-page href to check — the apply action now redirects
        # straight to `next`, so the response body is /coll/amend/900 itself.
        self.assertIn('action="/coll/amend/900"', body)

    def test_next_defaults_to_corrections_list(self):
        sup = self._login("sup", "pwS")
        self._raise_voucher_amount(sup)
        cid = self._corrections()[0]["id"]
        dist = self._login("dist", "pwD")
        status, body = self._get(dist, f"/coll/corrections/{cid}")
        self.assertIn('href="/coll/corrections"', body)

    def test_next_falls_back_safely_for_off_site_value(self):
        sup = self._login("sup", "pwS")
        self._raise_voucher_amount(sup)
        cid = self._corrections()[0]["id"]
        dist = self._login("dist", "pwD")
        status, body = self._post(dist, f"/coll/corrections/{cid}",
                                  {"action": "apply", "next": "https://evil.example/"})
        self.assertIn('href="/menu"', body)


# ---------------------------------------------------------------------------
# Voucher Amendment (iteration4): distributor-only raw editor, gated on any
# open master-data correction for the bill.
# ---------------------------------------------------------------------------

class TestAmendVoucher(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("dist", "distributor", "pwD")
        self._add_user("sup", "supervisor", "pwS")
        self._add_user("smA", "salesman", "pwA")
        self._add_user("smB", "salesman", "pwB")
        self._add_beat("beatA", "smA")
        self._add_beat("beatB", "smB")
        self._seed_voucher("900", "beatA", "smA", amount="100.00", balance="80.00")
        self.inst_id = self._add_installment("900", amount="20.00", salesman="smA")

    def _seed_voucher(self, bill_no, beat, salesman, amount, balance):
        conn = coll_store.get_db()
        try:
            conn.execute(
                "INSERT INTO vouchers (bill_no, date, amount, balance, beat, salesman,"
                " created_by, created_at) VALUES (?, '2026-01-01', ?, ?, ?, ?, 'test', 't')",
                (bill_no, amount, balance, beat, salesman))
            conn.commit()
        finally:
            conn.close()

    def _add_installment(self, bill_no, date="2026-01-01", amount="20.00", salesman="smA"):
        conn = coll_store.get_db()
        try:
            cur = conn.execute(
                "INSERT INTO installments (bill_no, date, amount, salesman, created_by, created_at)"
                " VALUES (?, ?, ?, ?, 'test', 't')", (bill_no, date, amount, salesman))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def _snapshot(self, bill_no):
        conn = coll_store.get_db()
        try:
            v = dict(conn.execute(
                "SELECT date, amount, balance, beat, salesman FROM vouchers WHERE bill_no = ?",
                (bill_no,)).fetchone())
            insts = [dict(r) for r in conn.execute(
                "SELECT id, date, amount, salesman FROM installments WHERE bill_no = ?"
                " ORDER BY id", (bill_no,))]
        finally:
            conn.close()
        return {"voucher": v, "installments": insts}

    def _post_amend(self, opener, bill_no, snapshot, voucher, installments, note=""):
        data = [("v_date", voucher["date"]), ("v_amount", voucher["amount"]),
                ("v_beat", voucher["beat"]), ("v_salesman", voucher["salesman"]),
                ("note", note), ("snapshot", json.dumps(snapshot))]
        for row in installments:
            token = str(row["id"]) if row.get("id") is not None else row["token"]
            data.append(("inst_row", token))
            data.append((f"inst_date_{token}", row["date"]))
            data.append((f"inst_amount_{token}", row["amount"]))
            data.append((f"inst_salesman_{token}", row["salesman"]))
            if row.get("delete"):
                data.append((f"inst_delete_{token}", "on"))
        return self._post(opener, f"/coll/amend/{bill_no}", data)

    def _raise_voucher_amount(self, opener, from_path, new_amount="120.00"):
        return self._post(opener, "/coll/correct/900",
                          {"action": "raise", "kind": "voucher_amount",
                           "new_amount": new_amount, "note": "n", "from": from_path})

    # -- basic access / rendering --

    def test_menu_card_distributor_only(self):
        dist = self._login("dist", "pwD")
        status, body = self._get(dist, "/menu")
        self.assertIn("Amend Voucher", body)
        sup = self._login("sup", "pwS")
        status, body = self._get(sup, "/menu")
        self.assertNotIn("Amend Voucher", body)

    def test_other_roles_refused(self):
        for name, pw in (("sup", "pwS"), ("smA", "pwA")):
            opener = self._login(name, pw)
            status, body = self._get(opener, "/coll/amend")
            self.assertIn("permission", body)
            status, body = self._get(opener, "/coll/amend/900")
            self.assertIn("permission", body)
            status, body = self._post(opener, "/coll/amend/900", {})
            self.assertIn("permission", body)

    def test_form_renders_fields_and_history(self):
        dist = self._login("dist", "pwD")
        status, body = self._get(dist, "/coll/amend/900")
        self.assertEqual(status, 200)
        self.assertIn("Voucher Amendment", body)
        self.assertIn('value="100.00"', body)
        self.assertIn('value="20.00"', body)
        self.assertIn(f'name="inst_date_{self.inst_id}"', body)
        self.assertIn("beatA", body)
        self.assertIn("smA", body)
        self.assertIn('name="snapshot"', body)

    def test_unknown_bill_refused(self):
        dist = self._login("dist", "pwD")
        status, body = self._get(dist, "/coll/amend/nosuch")
        self.assertIn("No voucher found", body)

    def test_completed_voucher_refused(self):
        conn = coll_store.get_db()
        conn.execute(
            "INSERT INTO completed_vouchers (bill_no, date, amount, balance, beat, salesman,"
            " created_by, created_at) VALUES"
            " ('901', '2026-01-01', '50.00', '0.00', 'beatA', 'smA', 't', 't')")
        conn.commit()
        conn.close()
        dist = self._login("dist", "pwD")
        status, body = self._get(dist, "/coll/amend/901")
        self.assertIn("cannot be amended", body)

    # -- happy path + staged refresh --

    def test_post_happy_path_updates_master_and_staged(self):
        stem = "coll20260101-beat_salesman-beatA_smA"
        report_path = self._write_staging_report(
            stem, "beatA", "smA", start="confirmed", submit="",
            vouchers=[{"bill_no": "900", "date": "2026-01-01", "balance": "80.00",
                       "payment": "", "payment_date": "", "beat": "beatA", "salesman": "smA"}])
        dist = self._login("dist", "pwD")
        snap = self._snapshot("900")
        voucher = {"date": "2026-02-01", "amount": "150.00", "beat": "beatB", "salesman": "smB"}
        installments = [{"id": self.inst_id, "date": "2026-01-01",
                         "amount": "20.00", "salesman": "smA"}]
        status, body = self._post_amend(dist, "900", snap, voucher, installments, note="fixed")
        self.assertIn("Amendment applied", body)
        conn = coll_store.get_db()
        row = dict(conn.execute(
            "SELECT amount, balance, beat, salesman FROM vouchers WHERE bill_no = '900'"
        ).fetchone())
        conn.close()
        self.assertEqual(row, {"amount": "150.00", "balance": "130.00",
                               "beat": "beatB", "salesman": "smB"})
        staged = json.loads(report_path.read_text(encoding="utf-8"))
        v = staged["vouchers"][0]
        self.assertEqual(v["balance"], "130.00")
        self.assertEqual(v["salesman"], "smB")
        self.assertEqual(v["beat"], "beatA")  # NOT refreshed — decision 7
        amds = coll_store.load_amendments(bill_no="900")
        self.assertEqual(len(amds), 1)
        self.assertEqual(amds[0]["note"], "fixed")

    def test_validation_failure_preserves_submitted_values(self):
        dist = self._login("dist", "pwD")
        snap = self._snapshot("900")
        voucher = {"date": "2026-02-01", "amount": "not-a-number",
                  "beat": "beatA", "salesman": "smA"}
        installments = [{"id": self.inst_id, "date": "2026-01-01",
                         "amount": "20.00", "salesman": "smA"}]
        status, body = self._post_amend(dist, "900", snap, voucher, installments)
        self.assertIn("must be a positive number", body)
        self.assertIn('value="not-a-number"', body)
        self.assertEqual(coll_store.load_amendments(bill_no="900"), [])

    def test_unknown_beat_rejected(self):
        dist = self._login("dist", "pwD")
        snap = self._snapshot("900")
        voucher = {"date": "2026-02-01", "amount": "100.00",
                  "beat": "nosuchbeat", "salesman": "smA"}
        installments = [{"id": self.inst_id, "date": "2026-01-01",
                         "amount": "20.00", "salesman": "smA"}]
        status, body = self._post_amend(dist, "900", snap, voucher, installments)
        self.assertIn("Unknown beat", body)
        self.assertEqual(coll_store.load_amendments(bill_no="900"), [])

    def test_conflict_reloads_current_data(self):
        dist = self._login("dist", "pwD")
        snap = self._snapshot("900")
        conn = coll_store.get_db()
        conn.execute("UPDATE vouchers SET amount = '999.00', balance = '979.00'"
                    " WHERE bill_no = '900'")
        conn.commit()
        conn.close()
        voucher = {"date": "2026-01-01", "amount": "100.00", "beat": "beatA", "salesman": "smA"}
        installments = [{"id": self.inst_id, "date": "2026-01-01",
                         "amount": "20.00", "salesman": "smA"}]
        status, body = self._post_amend(dist, "900", snap, voucher, installments)
        self.assertIn("changed while you were editing", body)
        self.assertIn('value="999.00"', body)
        self.assertEqual(coll_store.load_amendments(bill_no="900"), [])

    def test_combined_add_delete_edit_via_tokens(self):
        second_id = self._add_installment("900", amount="15.00", salesman="smA")
        dist = self._login("dist", "pwD")
        snap = self._snapshot("900")
        voucher = {"date": "2026-01-01", "amount": "100.00", "beat": "beatA", "salesman": "smA"}
        installments = [
            {"id": self.inst_id, "date": "2026-01-01", "amount": "35.00", "salesman": "smA"},
            {"id": second_id, "date": "2026-01-01", "amount": "15.00",
             "salesman": "smA", "delete": True},
            {"id": None, "token": "new1", "date": "2026-01-05",
             "amount": "10.00", "salesman": "smA"},
        ]
        status, body = self._post_amend(dist, "900", snap, voucher, installments)
        self.assertIn("Amendment applied", body)
        conn = coll_store.get_db()
        rows = {r["id"]: r["amount"] for r in conn.execute(
            "SELECT id, amount FROM installments WHERE bill_no = '900'")}
        conn.close()
        self.assertNotIn(second_id, rows)
        self.assertEqual(rows[self.inst_id], "35.00")
        self.assertEqual(len(rows), 2)

    # -- correction gate (decision 4) --

    def test_amend_blocked_by_open_master_correction(self):
        stem = "coll20260101-beat_salesman-beatA_smA"
        self._write_staging_report(
            stem, "beatA", "smA", start="new", submit="",
            vouchers=[{"bill_no": "900", "date": "2026-01-01", "balance": "80.00",
                       "payment": "", "payment_date": "", "beat": "beatA", "salesman": "smA"}])
        sup = self._login("sup", "pwS")
        self._raise_voucher_amount(sup, f"/coll/approve-start/{stem}")
        dist = self._login("dist", "pwD")
        status, body = self._get(dist, "/coll/amend/900")
        self.assertIn("Correction Request #", body)
        self.assertNotIn('name="v_amount"', body)

    def test_resolving_blocking_correction_unlocks_amend(self):
        stem = "coll20260101-beat_salesman-beatA_smA"
        self._write_staging_report(
            stem, "beatA", "smA", start="new", submit="",
            vouchers=[{"bill_no": "900", "date": "2026-01-01", "balance": "80.00",
                       "payment": "", "payment_date": "", "beat": "beatA", "salesman": "smA"}])
        sup = self._login("sup", "pwS")
        self._raise_voucher_amount(sup, f"/coll/approve-start/{stem}")
        cid = coll_store.load_corrections()[0]["id"]
        dist = self._login("dist", "pwD")
        status, body = self._get(dist, "/coll/amend/900")
        self.assertIn(f'action="/coll/corrections/{cid}"', body)
        status, body = self._post(dist, f"/coll/corrections/{cid}",
                                  {"action": "apply", "next": "/coll/amend/900"})
        self.assertIn("Correction applied", body)
        status, body = self._get(dist, "/coll/amend/900")
        self.assertIn("Voucher Amendment", body)
        self.assertIn('name="v_amount"', body)
        self.assertNotIn("Correction Request #", body)

    def test_two_open_corrections_resolve_one_then_other(self):
        stem = "coll20260101-beat_salesman-beatA_smA"
        self._write_staging_report(
            stem, "beatA", "smA", start="new", submit="",
            vouchers=[{"bill_no": "900", "date": "2026-01-01", "balance": "80.00",
                       "payment": "", "payment_date": "", "beat": "beatA", "salesman": "smA"}])
        sup = self._login("sup", "pwS")
        self._raise_voucher_amount(sup, f"/coll/approve-start/{stem}", new_amount="120.00")
        self._post(sup, "/coll/correct/900",
                  {"action": "raise", "kind": "installment_amount",
                   "installment_id": str(self.inst_id), "new_amount": "25.00",
                   "from": f"/coll/approve-start/{stem}"})
        corrs = coll_store.load_corrections()
        self.assertEqual(len(corrs), 2)
        first_id, second_id = sorted(c["id"] for c in corrs)
        dist = self._login("dist", "pwD")
        status, body = self._get(dist, "/coll/amend/900")
        self.assertIn(f'action="/coll/corrections/{first_id}"', body)
        self._post(dist, f"/coll/corrections/{first_id}",
                  {"action": "reject", "next": "/coll/amend/900"})
        status, body = self._get(dist, "/coll/amend/900")
        self.assertIn(f'action="/coll/corrections/{second_id}"', body)
        self._post(dist, f"/coll/corrections/{second_id}",
                  {"action": "reject", "next": "/coll/amend/900"})
        status, body = self._get(dist, "/coll/amend/900")
        self.assertIn("Voucher Amendment", body)
        self.assertIn('name="v_amount"', body)

    def test_collection_amount_correction_does_not_gate_amend(self):
        stem = "coll20260101-beat_salesman-beatA_smA"
        self._write_staging_report(
            stem, "beatA", "smA", start="confirmed", submit="submitted",
            vouchers=[{"bill_no": "900", "date": "2026-01-01", "balance": "80.00",
                       "payment": "10.00", "payment_date": "2026-01-01",
                       "beat": "beatA", "salesman": "smA"}])
        sup = self._login("sup", "pwS")
        self._post(sup, "/coll/correct/900",
                  {"action": "raise", "kind": "collection_amount",
                   "new_amount": "15.00", "note": "n",
                   "from": f"/coll/approve-submit/{stem}"})
        self.assertEqual(coll_store.load_corrections()[0]["kind"], "collection_amount")
        dist = self._login("dist", "pwD")
        status, body = self._get(dist, "/coll/amend/900")
        self.assertIn("Voucher Amendment", body)
        self.assertIn('name="v_amount"', body)
        self.assertNotIn("Correction Request #", body)


# ---------------------------------------------------------------------------
# Collection-amount corrections at Approve Collections: the only kind
# raiseable from the submit review; resolvable by supervisor OR distributor
# (or by Return + salesman revision, which auto-settles on match).
# ---------------------------------------------------------------------------

class TestCollectionCorrections(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("smA", "salesman", "pwA")
        self._add_user("sup", "supervisor", "pwS")
        self._add_user("dist", "distributor", "pwD")
        self._add_beat("beatA", "smA")
        self._add_voucher("900", "beatA", "smA", balance="50.00")
        self.stem = "coll20260101-beat_salesman-beatA_smA"
        self.report_path = self._write_staging_report(
            self.stem, "beatA", "smA", start="confirmed", submit="submitted",
            vouchers=[{"bill_no": "900", "date": "2026-01-01", "balance": "50.00",
                       "payment": "30.00", "payment_date": "2026-01-01",
                       "beat": "beatA", "salesman": "smA"}])
        self.from_path = f"/coll/approve-submit/{self.stem}"

    def _staged_voucher(self):
        return json.loads(self.report_path.read_text(encoding="utf-8"))["vouchers"][0]

    def _raise_collection(self, opener, new_amount="45.00"):
        return self._post(opener, "/coll/correct/900",
                          {"action": "raise", "kind": "collection_amount",
                           "new_amount": new_amount, "note": "physical shows different",
                           "from": self.from_path})

    def _complete_verification(self, opener):
        # Phase 4 hard-gates approve on physical-voucher verification too —
        # tests exercising a successful approve must satisfy it first.
        self._post(opener, self.from_path + "/verify", {"bill_no": "900", "verified": "1"})
        self._post(opener, self.from_path + "/verify", {"count": "1"})

    def test_submit_context_offers_only_collection_kind(self):
        opener = self._login("sup", "pwS")
        status, body = self._get(opener, f"/coll/correct/900?from={self.from_path}")
        self.assertEqual(status, 200)
        self.assertIn("Change the collection amount", body)
        self.assertNotIn("Change the voucher amount", body)
        self.assertNotIn("Change an installment amount", body)
        self.assertIn("Current collection", body)
        self.assertIn("30.00", body)

    def test_start_context_excludes_collection_kind(self):
        opener = self._login("sup", "pwS")
        status, body = self._get(opener, "/coll/correct/900?from=/coll/approve-start/x")
        self.assertIn("Change the voucher amount", body)
        self.assertNotIn("Change the collection amount", body)

    def test_master_kind_rejected_in_submit_context(self):
        opener = self._login("sup", "pwS")
        status, body = self._post(opener, "/coll/correct/900",
                                  {"action": "raise", "kind": "voucher_amount",
                                   "new_amount": "60.00", "from": self.from_path})
        self.assertIn("Choose what kind", body)
        self.assertEqual(coll_store.load_corrections(), [])

    def test_raise_snapshots_staged_payment(self):
        opener = self._login("sup", "pwS")
        status, _ = self._raise_collection(opener)
        self.assertEqual(status, 200)  # 303 followed back to the review
        corr = coll_store.load_corrections()[0]
        self.assertEqual(corr["kind"], "collection_amount")
        self.assertEqual(corr["old"], {"payment": "30.00", "date": "2026-01-01"})
        self.assertEqual(corr["new"], {"payment": "45.00"})
        self.assertEqual(corr["origin_stage"], "submit")

    def test_raise_refused_for_a_returns_voucher(self):
        data = json.loads(self.report_path.read_text(encoding="utf-8"))
        data["vouchers"][0].update(payment_type="returns", return_items=[
            {"item": "Soap", "qty": "3", "price": "10.00", "amount": "30.00"}])
        self.report_path.write_text(json.dumps(data), encoding="utf-8")
        opener = self._login("sup", "pwS")
        status, body = self._raise_collection(opener)
        self.assertIn("paid by returned stock", body)
        self.assertEqual(coll_store.load_corrections(), [])

    def test_raise_validation(self):
        opener = self._login("sup", "pwS")
        status, body = self._raise_collection(opener, new_amount="30.00")
        self.assertIn("same as the entered", body)
        status, body = self._raise_collection(opener, new_amount="60.00")  # > 50 balance
        self.assertIn("exceeds balance", body)
        self.assertEqual(coll_store.load_corrections(), [])

    def test_open_correction_gates_approve_submit(self):
        sup = self._login("sup", "pwS")
        self._raise_collection(sup)
        status, body = self._post(sup, self.from_path, {"action": "approve"})
        self.assertIn("Cannot approve", body)
        self.assertIn("1 correction request is awaiting resolution", body)
        saved = json.loads(self.report_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["stages"]["submit"], "submitted")
        status, body = self._get(sup, self.from_path)
        self.assertIn("Correction pending", body)

    def test_supervisor_can_apply_collection_correction(self):
        sup = self._login("sup", "pwS")
        self._raise_collection(sup)
        cid = coll_store.load_corrections()[0]["id"]
        status, body = self._get(sup, f"/coll/corrections/{cid}")
        self.assertIn('value="apply"', body)  # supervisor CAN act on this kind
        status, body = self._post(sup, f"/coll/corrections/{cid}", {"action": "apply"})
        self.assertIn("staged collection was updated", body)
        self.assertEqual(self._staged_voucher()["payment"], "45.00")
        # Approval unblocked (once verification is also satisfied).
        self._complete_verification(sup)
        status, body = self._post(sup, self.from_path, {"action": "approve"})
        self.assertIn("Collections approved", body)

    def test_supervisor_cannot_apply_master_kind(self):
        self._add_voucher("901", "beatA", "smA", balance="40.00")
        sup = self._login("sup", "pwS")
        self._post(sup, "/coll/correct/901",
                   {"action": "raise", "kind": "voucher_amount",
                    "new_amount": "55.00", "from": "/coll/approve-start/x"})
        cid = coll_store.load_corrections()[0]["id"]
        status, body = self._get(sup, f"/coll/corrections/{cid}")
        self.assertNotIn('value="apply"', body)
        status, body = self._post(sup, f"/coll/corrections/{cid}", {"action": "apply"})
        self.assertIn("permission", body)
        self.assertEqual(coll_store.load_correction(cid)["status"], "open")
        dist = self._login("dist", "pwD")
        status, body = self._post(dist, f"/coll/corrections/{cid}", {"action": "apply"})
        self.assertIn("master data updated", body)

    def test_distributor_can_apply_collection_correction(self):
        sup = self._login("sup", "pwS")
        self._raise_collection(sup)
        cid = coll_store.load_corrections()[0]["id"]
        dist = self._login("dist", "pwD")
        status, body = self._post(dist, f"/coll/corrections/{cid}", {"action": "apply"})
        self.assertIn("staged collection was updated", body)
        self.assertEqual(self._staged_voucher()["payment"], "45.00")

    def test_return_then_matching_resubmit_auto_settles(self):
        sup = self._login("sup", "pwS")
        self._raise_collection(sup)
        cid = coll_store.load_corrections()[0]["id"]
        # Return works despite the open request.
        status, body = self._post(sup, self.from_path, {"action": "return"})
        self.assertIn("returned", body)
        # Salesman revises to the requested amount and resubmits.
        sm = self._login("smA", "pwA")
        status, body = self._post(sm, f"/coll/submit/{self.stem}",
                                  {"action": "submit", "pay_900": "45.00"})
        self.assertIn("submitted for supervisor review", body)
        corr = coll_store.load_correction(cid)
        self.assertEqual(corr["status"], "applied")
        self.assertEqual(corr["resolution_note"], "matched after salesman revision")
        # Approval now goes through (once verification is also satisfied).
        self._complete_verification(sup)
        status, body = self._post(sup, self.from_path, {"action": "approve"})
        self.assertIn("Collections approved", body)

    def test_resubmit_with_other_value_keeps_request_open(self):
        sup = self._login("sup", "pwS")
        self._raise_collection(sup)
        cid = coll_store.load_corrections()[0]["id"]
        self._post(sup, self.from_path, {"action": "return"})
        sm = self._login("smA", "pwA")
        self._post(sm, f"/coll/submit/{self.stem}",
                   {"action": "submit", "pay_900": "40.00"})
        self.assertEqual(coll_store.load_correction(cid)["status"], "open")
        status, body = self._post(sup, self.from_path, {"action": "approve"})
        self.assertIn("Cannot approve", body)

    def test_correction_to_empty_clears_collection(self):
        sup = self._login("sup", "pwS")
        self._raise_collection(sup, new_amount="")
        cid = coll_store.load_corrections()[0]["id"]
        self.assertEqual(coll_store.load_correction(cid)["new"], {"payment": ""})
        self._post(sup, f"/coll/corrections/{cid}", {"action": "apply"})
        v = self._staged_voucher()
        self.assertEqual((v["payment"], v["payment_date"]), ("", ""))

    def test_salesman_cannot_act(self):
        sup = self._login("sup", "pwS")
        self._raise_collection(sup)
        cid = coll_store.load_corrections()[0]["id"]
        sm = self._login("smA", "pwA")
        status, body = self._post(sm, f"/coll/corrections/{cid}", {"action": "apply"})
        self.assertIn("permission", body)
        self.assertEqual(coll_store.load_correction(cid)["status"], "open")


# ---------------------------------------------------------------------------
# Physical-voucher verification on Approve Collections (Phase 4): mirror of
# TestStartVerification for the evening cross-check — same "verification"
# key, guarded on stages.submit == "submitted", independent hard gate that
# combines with (but doesn't replace) the open-corrections gate.
# ---------------------------------------------------------------------------

class TestSubmitVerification(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("smA", "salesman", "pwA")
        self._add_user("sup", "supervisor", "pwS")
        self._add_voucher("900", "beatA", "smA", balance="50.00")
        self._add_voucher("901", "beatA", "smA", balance="30.00")
        self._add_beat("beatA", "smA")
        self.stem = "coll20260101-beat_salesman-beatA_smA"
        self.path = self._write_staging_report(
            self.stem, "beatA", "smA", start="confirmed", submit="submitted",
            vouchers=[
                {"bill_no": "900", "date": "2026-01-01", "balance": "50.00",
                 "payment": "20.00", "payment_date": "2026-01-01",
                 "beat": "beatA", "salesman": "smA"},
                {"bill_no": "901", "date": "2026-01-01", "balance": "30.00",
                 "payment": "", "payment_date": "", "beat": "beatA", "salesman": "smA"},
            ])
        self.verify_url = f"/coll/approve-submit/{self.stem}/verify"

    def _saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _complete_verification(self, opener):
        for bill in ("900", "901"):
            self._post(opener, self.verify_url, {"bill_no": bill, "verified": "1"})
        self._post(opener, self.verify_url, {"count": "1"})

    def test_toggle_persists_and_untoggle_removes(self):
        opener = self._login("sup", "pwS")
        status, body = self._post(opener, self.verify_url,
                                  {"bill_no": "900", "verified": "1"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(self._saved()["verification"]["bill_nos"], ["900"])
        self._post(opener, self.verify_url, {"bill_no": "900", "verified": "0"})
        self.assertEqual(self._saved()["verification"]["bill_nos"], [])

    def test_count_flag_roundtrip(self):
        opener = self._login("sup", "pwS")
        self._post(opener, self.verify_url, {"count": "1"})
        self.assertTrue(self._saved()["verification"]["count"])
        self._post(opener, self.verify_url, {"count": "0"})
        self.assertFalse(self._saved()["verification"]["count"])

    def test_review_renders_persisted_state(self):
        opener = self._login("sup", "pwS")
        self._post(opener, self.verify_url, {"bill_no": "900", "verified": "1"})
        status, body = self._get(opener, f"/coll/approve-submit/{self.stem}")
        self.assertEqual(status, 200)
        self.assertIn("Verified 1 / 2", body)
        self.assertIn("checked", body)

    def test_wrong_stage_returns_409(self):
        self._write_staging_report(self.stem, "beatA", "smA",
                                   start="confirmed", submit="confirmed")
        opener = self._login("sup", "pwS")
        status, _ = self._post(opener, self.verify_url,
                               {"bill_no": "900", "verified": "1"})
        self.assertEqual(status, 409)

    def test_unknown_bill_returns_404(self):
        opener = self._login("sup", "pwS")
        status, _ = self._post(opener, self.verify_url,
                               {"bill_no": "999", "verified": "1"})
        self.assertEqual(status, 404)

    def test_param_misuse_returns_400(self):
        opener = self._login("sup", "pwS")
        status, _ = self._post(opener, self.verify_url, {})
        self.assertEqual(status, 400)
        status, _ = self._post(opener, self.verify_url,
                               {"bill_no": "900", "verified": "1", "count": "1"})
        self.assertEqual(status, 400)

    def test_salesman_gets_403(self):
        opener = self._login("smA", "pwA")
        status, _ = self._post(opener, self.verify_url,
                               {"bill_no": "900", "verified": "1"})
        self.assertEqual(status, 403)
        self.assertNotIn("verification", self._saved())

    def test_logged_out_gets_401(self):
        opener = self._client()
        status, _ = self._post(opener, self.verify_url,
                               {"bill_no": "900", "verified": "1"})
        self.assertEqual(status, 401)

    def test_approve_blocked_until_verification_complete(self):
        opener = self._login("sup", "pwS")
        status, body = self._post(opener, f"/coll/approve-submit/{self.stem}",
                                  {"action": "approve"})
        self.assertEqual(status, 200)
        self.assertIn("Cannot approve", body)
        self.assertEqual(self._saved()["stages"]["submit"], "submitted")

        for bill in ("900", "901"):
            self._post(opener, self.verify_url, {"bill_no": bill, "verified": "1"})
        status, body = self._post(opener, f"/coll/approve-submit/{self.stem}",
                                  {"action": "approve"})
        self.assertIn("Cannot approve", body)
        self.assertEqual(self._saved()["stages"]["submit"], "submitted")

    def test_complete_verification_approves_and_pops_key(self):
        opener = self._login("sup", "pwS")
        self._complete_verification(opener)
        status, body = self._post(opener, f"/coll/approve-submit/{self.stem}",
                                  {"action": "approve"})
        self.assertEqual(status, 200)
        self.assertIn("Collections approved", body)
        saved = self._saved()
        self.assertEqual(saved["stages"]["submit"], "confirmed")
        self.assertNotIn("verification", saved)

    def test_return_needs_no_verification(self):
        opener = self._login("sup", "pwS")
        status, body = self._post(opener, f"/coll/approve-submit/{self.stem}",
                                  {"action": "return"})
        self.assertEqual(status, 200)
        self.assertIn("returned", body)
        saved = self._saved()
        self.assertEqual(saved["stages"]["submit"], "returned")
        self.assertNotIn("verification", saved)

    def test_open_correction_blocks_even_with_full_verification(self):
        opener = self._login("sup", "pwS")
        self._complete_verification(opener)
        self._post(opener, "/coll/correct/900",
                  {"action": "raise", "kind": "collection_amount",
                   "new_amount": "25.00", "from": f"/coll/approve-submit/{self.stem}"})
        status, body = self._post(opener, f"/coll/approve-submit/{self.stem}",
                                  {"action": "approve"})
        self.assertIn("correction request", body)
        self.assertEqual(self._saved()["stages"]["submit"], "submitted")
        cid = coll_store.load_corrections()[0]["id"]
        self._post(opener, f"/coll/corrections/{cid}", {"action": "reject"})
        status, body = self._post(opener, f"/coll/approve-submit/{self.stem}",
                                  {"action": "approve"})
        self.assertIn("Collections approved", body)


# ---------------------------------------------------------------------------
# Amendment Requests: a raise->resolve lifecycle in front of Voucher
# Amendment. Free-text note only (no structured kind/snapshot like
# corrections); auto-resolves when the distributor lands ANY amendment on
# the bill; the distributor may instead reject without amending, and the
# raiser may withdraw their own open request. Not stage-gated.
# ---------------------------------------------------------------------------

class TestAmendmentRequests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("dist", "distributor", "pwD")
        self._add_user("sup", "supervisor", "pwS")
        self._add_user("smA", "salesman", "pwA")
        self._add_beat("beatA", "smA")
        self._add_voucher("900", "beatA", "smA", balance="100.00")

    def _requests(self):
        return coll_store.load_amendment_requests()

    def _raise(self, opener, bill_no="900", note="date looks wrong", from_path=None):
        data = {"action": "raise", "note": note}
        if from_path is not None:
            data["from"] = from_path
        return self._post(opener, f"/coll/amend-request/{bill_no}", data)

    def test_menu_cards(self):
        sup = self._login("sup", "pwS")
        status, body = self._get(sup, "/menu")
        self.assertIn("Request Voucher Amendment", body)
        self.assertIn("Amendment Requests", body)

        dist = self._login("dist", "pwD")
        status, body = self._get(dist, "/menu")
        self.assertNotIn("Request Voucher Amendment", body)  # distributor can't raise
        self.assertIn("Amendment Requests", body)  # but can view/resolve

    def test_distributor_cannot_raise(self):
        dist = self._login("dist", "pwD")
        status, body = self._get(dist, "/coll/amend-request/900")
        self.assertIn("permission", body)
        status, body = self._raise(dist)
        self.assertIn("permission", body)
        self.assertEqual(self._requests(), [])

    def test_salesman_can_raise(self):
        sm = self._login("smA", "pwA")
        status, body = self._raise(sm, note="balance seems off")
        self.assertEqual(status, 200)  # 303 followed
        reqs = self._requests()
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]["note"], "balance seems off")
        self.assertEqual(reqs[0]["requested_by"], "smA")
        self.assertEqual(reqs[0]["status"], "open")

    def test_raise_requires_note(self):
        sup = self._login("sup", "pwS")
        status, body = self._raise(sup, note="")
        self.assertIn("Describe what needs", body)
        self.assertEqual(self._requests(), [])

    def test_completed_voucher_refused(self):
        conn = coll_store.get_db()
        conn.execute(
            "INSERT INTO completed_vouchers (bill_no, date, amount, balance, beat, salesman,"
            " created_by, created_at) VALUES"
            " ('901', '2026-01-01', '50.00', '0.00', 'beatA', 'smA', 't', 't')")
        conn.commit()
        conn.close()
        sup = self._login("sup", "pwS")
        status, body = self._get(sup, "/coll/amend-request/901")
        self.assertIn("cannot have an amendment requested", body)

    def test_withdraw_own_requests_only(self):
        sup = self._login("sup", "pwS")
        self._raise(sup)
        rid = self._requests()[0]["id"]

        dist = self._login("dist", "pwD")
        status, body = self._post(dist, "/coll/amend-request/900",
                                  {"action": "withdraw", "req_id": str(rid)})
        self.assertIn("permission", body)  # distributor has no raise permission at all

        sm = self._login("smA", "pwA")
        status, body = self._post(sm, "/coll/amend-request/900",
                                  {"action": "withdraw", "req_id": str(rid)})
        self.assertIn("Only your own", body)
        self.assertEqual(coll_store.load_amendment_request(rid)["status"], "open")

        status, body = self._post(sup, "/coll/amend-request/900",
                                  {"action": "withdraw", "req_id": str(rid)})
        self.assertEqual(coll_store.load_amendment_request(rid)["status"], "withdrawn")

    def test_list_shows_active_and_history(self):
        sup = self._login("sup", "pwS")
        self._raise(sup, note="please check date")
        dist = self._login("dist", "pwD")
        status, body = self._get(dist, "/coll/amend-requests")
        self.assertIn("900", body)
        self.assertIn("please check date", body)
        self.assertIn("Pending", body)

    def test_reject_without_amending(self):
        sup = self._login("sup", "pwS")
        self._raise(sup)
        rid = self._requests()[0]["id"]
        dist = self._login("dist", "pwD")
        status, body = self._get(dist, f"/coll/amend-requests/{rid}")
        self.assertIn('value="reject"', body)
        status, body = self._post(dist, f"/coll/amend-requests/{rid}",
                                  {"action": "reject", "resolution_note": "not needed"})
        self.assertIn("rejected", body)
        req = coll_store.load_amendment_request(rid)
        self.assertEqual(req["status"], "rejected")
        self.assertEqual(req["resolved_by"], "dist")

        # Supervisor holds raise_amendment_request but not amend_voucher —
        # cannot resolve, only raise/withdraw.
        self._raise(sup, note="second")
        rid2 = [r for r in self._requests() if r["status"] == "open"][0]["id"]
        status, body = self._post(sup, f"/coll/amend-requests/{rid2}", {"action": "reject"})
        self.assertIn("permission", body)

    def test_auto_resolve_on_amendment(self):
        sup = self._login("sup", "pwS")
        self._raise(sup, note="date wrong")
        rid = self._requests()[0]["id"]

        dist = self._login("dist", "pwD")
        status, body = self._get(dist, "/coll/amend/900")
        self.assertIn("Open amendment request", body)
        self.assertIn("date wrong", body)

        snap = {"voucher": {"date": "2026-01-01", "amount": "100.00", "balance": "100.00",
                            "beat": "beatA", "salesman": "smA"}, "installments": []}
        data = [("v_date", "2026-02-01"), ("v_amount", "100.00"), ("v_beat", "beatA"),
                ("v_salesman", "smA"), ("note", "fixed date"), ("snapshot", json.dumps(snap))]
        status, body = self._post(dist, "/coll/amend/900", data)
        self.assertIn("Amendment applied", body)

        req = coll_store.load_amendment_request(rid)
        self.assertEqual(req["status"], "applied")
        self.assertEqual(req["resolved_by"], "dist")
        self.assertIsNotNone(req["linked_amendment_id"])

    def test_correction_form_cross_link_only_on_approve_collection_list(self):
        stem = "coll20260101-beat_salesman-beatA_smA"
        self._write_staging_report(
            stem, "beatA", "smA", start="new", submit="",
            vouchers=[{"bill_no": "900", "date": "2026-01-01", "balance": "100.00",
                       "payment": "", "payment_date": "", "beat": "beatA", "salesman": "smA"}])
        sup = self._login("sup", "pwS")
        status, body = self._get(sup, f"/coll/correct/900?from=/coll/approve-start/{stem}")
        self.assertIn("Raise an Amendment Request instead", body)

        # Approve Collections context (collection_amount only) — no cross-link.
        self._write_staging_report(
            stem, "beatA", "smA", start="confirmed", submit="submitted",
            vouchers=[{"bill_no": "900", "date": "2026-01-01", "balance": "100.00",
                       "payment": "10.00", "payment_date": "2026-01-01",
                       "beat": "beatA", "salesman": "smA"}])
        status, body = self._get(sup, f"/coll/correct/900?from=/coll/approve-submit/{stem}")
        self.assertNotIn("Raise an Amendment Request instead", body)


# ---------------------------------------------------------------------------
# Manage Users / Profile — permission gating + full HTTP lifecycle
# ---------------------------------------------------------------------------

class TestManageUsersAndProfile(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("dist", "distributor", "distpass1")
        self._add_user("sup", "supervisor", "suppass1")
        self._add_user("sm", "salesman", "smpass1")

    # ---- permission gating ----

    def test_salesman_blocked_from_manage_users(self):
        opener = self._login("sm", "smpass1")
        status, body = self._get(opener, "/manage/users")
        self.assertIn("have permission for this action", body)

    def test_supervisor_blocked_from_manage_users(self):
        opener = self._login("sup", "suppass1")
        status, body = self._get(opener, "/manage/users")
        self.assertIn("have permission for this action", body)

    def test_distributor_allowed_manage_users(self):
        opener = self._login("dist", "distpass1")
        status, body = self._get(opener, "/manage/users")
        self.assertEqual(status, 200)
        self.assertIn("Manage Users", body)

    def test_profile_reachable_by_every_role(self):
        for name, pw in (("dist", "distpass1"), ("sup", "suppass1"), ("sm", "smpass1")):
            opener = self._login(name, pw)
            status, body = self._get(opener, "/profile")
            self.assertEqual(status, 200)
            self.assertIn("Change Password", body)

    # ---- create/edit/delete/reset lifecycle ----

    def test_create_user_end_to_end(self):
        dist = self._login("dist", "distpass1")
        status, body = self._post(dist, "/manage/users/new", {
            "name": "newsm", "role": "salesman",
            "password": "initpass1", "confirm_password": "initpass1",
        })
        self.assertEqual(status, 200)
        status, body = self._get(dist, "/manage/users")
        self.assertIn("newsm", body)

    def test_create_user_duplicate_shows_error(self):
        dist = self._login("dist", "distpass1")
        self._post(dist, "/manage/users/new", {
            "name": "dupuser", "role": "salesman",
            "password": "initpass1", "confirm_password": "initpass1",
        })
        status, body = self._post(dist, "/manage/users/new", {
            "name": "dupuser", "role": "salesman",
            "password": "initpass1", "confirm_password": "initpass1",
        })
        self.assertEqual(status, 200)
        self.assertIn("already exists", body)

    def test_edit_user_role(self):
        dist = self._login("dist", "distpass1")
        status, body = self._post(dist, "/manage/users/sm/edit", {"role": "supervisor"})
        self.assertEqual(status, 200)
        self.assertEqual(coll_store.load_user("sm")["role"], "supervisor")

    def test_edit_role_last_distributor_blocked(self):
        dist = self._login("dist", "distpass1")
        status, body = self._post(dist, "/manage/users/dist/edit", {"role": "supervisor"})
        self.assertEqual(status, 200)
        self.assertIn("cannot be changed", body)
        self.assertEqual(coll_store.load_user("dist")["role"], "distributor")

    def test_create_user_distributor_role_rejected(self):
        dist = self._login("dist", "distpass1")
        status, body = self._post(dist, "/manage/users/new", {
            "name": "seconddist", "role": "distributor",
            "password": "initpass1", "confirm_password": "initpass1",
        })
        self.assertEqual(status, 200)
        self.assertIsNone(coll_store.load_user("seconddist"))

    def test_promote_to_distributor_blocked(self):
        dist = self._login("dist", "distpass1")
        status, body = self._post(dist, "/manage/users/sm/edit", {"role": "distributor"})
        self.assertEqual(status, 200)
        self.assertEqual(coll_store.load_user("sm")["role"], "salesman")

    def test_manage_users_list_hides_edit_and_delete_for_distributor(self):
        dist = self._login("dist", "distpass1")
        status, body = self._get(dist, "/manage/users")
        self.assertEqual(status, 200)
        self.assertIn("/manage/users/dist/reset-password", body)
        self.assertNotIn("/manage/users/dist/edit", body)
        self.assertNotIn("/manage/users/dist/delete", body)

    def test_delete_unreferenced_user_succeeds(self):
        dist = self._login("dist", "distpass1")
        self._add_user("throwaway", "salesman", "pwpwpw1")
        status, body = self._post(dist, "/manage/users/throwaway/delete", {})
        self.assertEqual(status, 200)
        self.assertIsNone(coll_store.load_user("throwaway"))

    def test_delete_referenced_user_blocked(self):
        dist = self._login("dist", "distpass1")
        self._add_beat("beatX", "sm")
        status, body = self._post(dist, "/manage/users/sm/delete", {})
        self.assertEqual(status, 200)
        self.assertIn("referenced", body)
        self.assertIsNotNone(coll_store.load_user("sm"))

    def test_self_lockout_blocked(self):
        dist = self._login("dist", "distpass1")
        self._add_user("dist2", "distributor", "distpass2")
        status, body = self._post(dist, "/manage/users/dist/delete", {})
        self.assertEqual(status, 200)
        self.assertIn("own account", body)
        self.assertIsNotNone(coll_store.load_user("dist"))

    def test_reset_password_forces_change_on_next_login(self):
        dist = self._login("dist", "distpass1")
        status, body = self._post(dist, "/manage/users/sm/reset-password", {
            "new_password": "resetpass1", "confirm_password": "resetpass1",
        })
        self.assertEqual(status, 200)
        opener = self._login("sm", "resetpass1")
        status, body = self._get(opener, "/menu")
        self.assertIn("must change your password", body)

    # ---- forced first-login change: full lifecycle ----

    def test_forced_change_lifecycle(self):
        dist = self._login("dist", "distpass1")
        self._post(dist, "/manage/users/new", {
            "name": "freshuser", "role": "salesman",
            "password": "initpass1", "confirm_password": "initpass1",
        })

        opener = self._login("freshuser", "initpass1")
        status, body = self._get(opener, "/menu")
        self.assertEqual(status, 200)
        self.assertIn("must change your password", body)
        self.assertIn("Change Password", body)

        # A different route also bounces back to /profile — no routing around it.
        status, body = self._get(opener, "/reports")
        self.assertIn("must change your password", body)

        status, body = self._post(opener, "/profile/change-password", {
            "current_password": "initpass1",
            "new_password": "changedpass1",
            "confirm_password": "changedpass1",
        })
        self.assertEqual(status, 200)

        # Normal access resumes without re-login.
        status, body = self._get(opener, "/menu")
        self.assertEqual(status, 200)
        self.assertIn("Main Menu", body)
        self.assertNotIn("must change your password", body)

        row = coll_store.load_user("freshuser")
        self.assertFalse(row["must_change_password"])

    def test_change_password_wrong_current_shows_error(self):
        opener = self._login("sm", "smpass1")
        status, body = self._post(opener, "/profile/change-password", {
            "current_password": "wrongpass",
            "new_password": "changedpass1",
            "confirm_password": "changedpass1",
        })
        self.assertEqual(status, 200)
        self.assertIn("current password is incorrect", body)


# ---------------------------------------------------------------------------
# Manage Beats — permission gating + full HTTP lifecycle
# ---------------------------------------------------------------------------

class TestManageBeats(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("dist", "distributor", "distpass1")
        self._add_user("sm1", "salesman", "smpass1")
        self._add_user("sm2", "salesman", "smpass2")

    def test_salesman_blocked_from_manage_beats(self):
        opener = self._login("sm1", "smpass1")
        status, body = self._get(opener, "/manage/beats")
        self.assertIn("have permission for this action", body)

    def test_distributor_allowed_manage_beats(self):
        opener = self._login("dist", "distpass1")
        status, body = self._get(opener, "/manage/beats")
        self.assertEqual(status, 200)
        self.assertIn("Manage Beats", body)

    def test_create_beat_end_to_end(self):
        dist = self._login("dist", "distpass1")
        status, body = self._post(dist, "/manage/beats/new", {"name": "beatX", "salesman": "sm1"})
        self.assertEqual(status, 200)
        status, body = self._get(dist, "/manage/beats")
        self.assertIn("beatX", body)

    def test_edit_beat_salesman(self):
        dist = self._login("dist", "distpass1")
        self._add_beat("beatX", "sm1")
        status, body = self._post(dist, "/manage/beats/beatX/edit", {"salesman": "sm2"})
        self.assertEqual(status, 200)
        beats = coll_store.load_beats_raw()
        self.assertEqual(next(b["salesman"] for b in beats if b["name"] == "beatX"), "sm2")

    def test_delete_unreferenced_beat_succeeds(self):
        dist = self._login("dist", "distpass1")
        self._add_beat("beatX", "sm1")
        status, body = self._post(dist, "/manage/beats/beatX/delete", {})
        self.assertEqual(status, 200)
        self.assertEqual(coll_store.load_beats_raw(), [])

    def test_delete_referenced_beat_blocked(self):
        dist = self._login("dist", "distpass1")
        self._add_beat("beatX", "sm1")
        self._add_voucher("100", "beatX", "sm1")
        status, body = self._post(dist, "/manage/beats/beatX/delete", {})
        self.assertEqual(status, 200)
        self.assertIn("existing vouchers", body)
        self.assertEqual(len(coll_store.load_beats_raw()), 1)


# ---------------------------------------------------------------------------
# Submit Collections — payment type (cash/upi/check) round-trip + validation
# ---------------------------------------------------------------------------

class TestCollSubmitPaymentType(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("smA", "salesman", "pwA")
        self.stem = "coll20260101-beat_salesman-beatA_smA"
        self._write_staging_report(
            self.stem, "beatA", "smA",
            vouchers=[
                {"bill_no": "100", "date": "2026-01-01", "balance": "50.00",
                 "payment": "", "payment_date": "", "beat": "beatA", "salesman": "smA"},
                {"bill_no": "200", "date": "2026-01-01", "balance": "75.00",
                 "payment": "", "payment_date": "", "beat": "beatA", "salesman": "smA"},
            ],
        )

    def test_upi_payment_saved_and_redisplayed(self):
        opener = self._login("smA", "pwA")
        status, body = self._post(opener, f"/coll/submit/{self.stem}", {
            "action": "save",
            "pay_100": "20.00", "paytype_100": "upi", "upitxn_100": "TXN123",
            "pay_200": "", "paytype_200": "cash",
        })
        self.assertEqual(status, 200)
        status, body = self._get(opener, f"/coll/submit/{self.stem}")
        self.assertIn("TXN123", body)
        installments, _ = coll_store._load_installments(self.tmp / "staging" / f"{self.stem}.json")
        self.assertEqual(installments["100"]["payment_type"], "upi")
        self.assertEqual(installments["100"]["upi_txn_id"], "TXN123")

    def test_check_payment_saved_and_redisplayed(self):
        opener = self._login("smA", "pwA")
        status, body = self._post(opener, f"/coll/submit/{self.stem}", {
            "action": "save",
            "pay_100": "20.00", "paytype_100": "check",
            "checkbank_100": "Bank A", "checkbranch_100": "Main",
            "checkno_100": "CHK1", "checkdate_100": "2026-08-01",
        })
        self.assertEqual(status, 200)
        installments, _ = coll_store._load_installments(self.tmp / "staging" / f"{self.stem}.json")
        self.assertEqual(installments["100"]["payment_type"], "check")
        self.assertEqual(installments["100"]["check_bank"], "Bank A")
        self.assertEqual(installments["100"]["check_no"], "CHK1")

    def test_upi_without_txn_id_rejected(self):
        opener = self._login("smA", "pwA")
        status, body = self._post(opener, f"/coll/submit/{self.stem}", {
            "action": "save", "pay_100": "20.00", "paytype_100": "upi",
        })
        self.assertEqual(status, 200)
        self.assertIn("UPI transaction id is required", body)
        installments, _ = coll_store._load_installments(self.tmp / "staging" / f"{self.stem}.json")
        self.assertEqual(installments, {})

    def test_check_without_bank_rejected(self):
        opener = self._login("smA", "pwA")
        status, body = self._post(opener, f"/coll/submit/{self.stem}", {
            "action": "save", "pay_100": "20.00", "paytype_100": "check",
        })
        self.assertEqual(status, 200)
        self.assertIn("check bank is required", body)

    def _returns_form(self, rows, **extra):
        data = [("action", "save"), ("paytype_100", "returns"), ("pay_100", "999.00")]
        for item, qty, price in rows:
            data += [("retitem_100", item), ("retqty_100", qty), ("retprice_100", price)]
        data += list(extra.items())
        return data

    def test_returns_total_becomes_the_payment_and_redisplays(self):
        opener = self._login("smA", "pwA")
        status, body = self._post(opener, f"/coll/submit/{self.stem}", self._returns_form([
            ("Soap", "3", "4.50"), ("Tea", "2", "10"), ("", "", ""),
        ]))
        self.assertEqual(status, 200)
        installments, _ = coll_store._load_installments(self.tmp / "staging" / f"{self.stem}.json")
        entry = installments["100"]
        # 3 x 4.50 + 2 x 10 = 33.50; the posted "999.00" pay_ value is ignored.
        self.assertEqual(entry["payment"], "33.50")
        self.assertEqual(entry["payment_type"], "returns")
        self.assertEqual([i["item"] for i in entry["return_items"]], ["Soap", "Tea"])
        self.assertEqual(entry["return_items"][0]["amount"], "13.50")
        status, body = self._get(opener, f"/coll/submit/{self.stem}")
        self.assertIn("Soap", body)
        self.assertIn("Tea", body)

    def test_returns_over_balance_rejected(self):
        opener = self._login("smA", "pwA")
        status, body = self._post(opener, f"/coll/submit/{self.stem}",
                                  self._returns_form([("Soap", "11", "5")]))
        self.assertIn("exceeds balance", body)
        installments, _ = coll_store._load_installments(self.tmp / "staging" / f"{self.stem}.json")
        self.assertEqual(installments, {})

    def test_returns_invalid_item_rejected_and_rows_kept(self):
        opener = self._login("smA", "pwA")
        status, body = self._post(opener, f"/coll/submit/{self.stem}",
                                  self._returns_form([("Soap", "1.5", "5")]))
        self.assertIn("quantity must be a whole number", body)
        self.assertIn('value="Soap"', body)
        installments, _ = coll_store._load_installments(self.tmp / "staging" / f"{self.stem}.json")
        self.assertEqual(installments, {})

    def test_returns_with_no_items_records_no_payment(self):
        opener = self._login("smA", "pwA")
        status, body = self._post(opener, f"/coll/submit/{self.stem}",
                                  self._returns_form([("", "", "")]))
        self.assertEqual(status, 200)
        installments, _ = coll_store._load_installments(self.tmp / "staging" / f"{self.stem}.json")
        self.assertEqual(installments, {})

    def test_switching_away_from_returns_drops_items(self):
        opener = self._login("smA", "pwA")
        self._post(opener, f"/coll/submit/{self.stem}",
                   self._returns_form([("Soap", "1", "5")]))
        self._post(opener, f"/coll/submit/{self.stem}", {
            "action": "save", "pay_100": "5.00", "paytype_100": "cash"})
        installments, _ = coll_store._load_installments(self.tmp / "staging" / f"{self.stem}.json")
        self.assertEqual(installments["100"]["payment_type"], "cash")
        self.assertNotIn("return_items", installments["100"])

    def test_item_text_is_escaped_on_redisplay(self):
        opener = self._login("smA", "pwA")
        self._post(opener, f"/coll/submit/{self.stem}",
                   self._returns_form([("<script>alert(1)</script>", "1", "5")]))
        status, body = self._get(opener, f"/coll/submit/{self.stem}")
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)


# ---------------------------------------------------------------------------
# Checks screen — view for everyone with view_checks, resolve for distributor
# ---------------------------------------------------------------------------

class TestCollChecks(ApiTestCase):
    def setUp(self):
        super().setUp()
        self._add_user("smA", "salesman", "pwA")
        self._add_user("sup", "supervisor", "pwS")
        self._add_user("dist", "distributor", "pwD")
        self._add_voucher("100", "beatA", "smA", balance="100.00")
        coll_store.apply_post_to_db([{
            "bill_no": "100", "payment": "40.00", "salesman": "smA", "beat": "beatA",
            "payment_type": "check", "check_bank": "Bank A", "check_no": "CHK1",
            # Deliberately in the past: makes this check "overdue" regardless
            # of the real date the test suite runs on, so the menu banner
            # test below is deterministic.
            "check_date": "2020-01-01",
        }])
        self.check_id = coll_store.load_checks()[0]["id"]

    def test_salesman_can_view_but_not_resolve(self):
        opener = self._login("smA", "pwA")
        status, body = self._get(opener, "/coll/checks")
        self.assertEqual(status, 200)
        self.assertIn("CHK1", body)
        self.assertNotIn("Mark Encashed", body)

    def test_distributor_sees_resolve_actions(self):
        opener = self._login("dist", "pwD")
        status, body = self._get(opener, "/coll/checks")
        self.assertEqual(status, 200)
        self.assertIn("Mark Encashed", body)
        self.assertIn("Mark Bounced", body)

    def test_salesman_cannot_post_resolve_action(self):
        opener = self._login("smA", "pwA")
        status, body = self._post(opener, f"/coll/checks/{self.check_id}",
                                  {"action": "encash"})
        self.assertIn("have permission for this action", body)
        self.assertEqual(coll_store.load_check(self.check_id)["status"], "pending")

    def test_distributor_can_encash(self):
        opener = self._login("dist", "pwD")
        status, body = self._post(opener, f"/coll/checks/{self.check_id}",
                                  {"action": "encash"})
        self.assertEqual(coll_store.load_check(self.check_id)["status"], "encashed")

    def test_distributor_can_bounce_and_balance_restored(self):
        opener = self._login("dist", "pwD")
        status, body = self._post(opener, f"/coll/checks/{self.check_id}",
                                  {"action": "bounce", "resolution_note": "NSF"})
        check = coll_store.load_check(self.check_id)
        self.assertEqual(check["status"], "bounced")
        self.assertEqual(check["resolution_note"], "NSF")
        conn = coll_store.get_db()
        try:
            row = conn.execute(
                "SELECT balance FROM vouchers WHERE bill_no='100'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["balance"], "100.00")

    def test_menu_shows_check_banner_for_view_checks_role(self):
        opener = self._login("smA", "pwA")
        status, body = self._get(opener, "/menu")
        self.assertIn("View Checks", body)


# ---------------------------------------------------------------------------
# First-time registration + distributor "Forgot password?" (secret question)
# ---------------------------------------------------------------------------

class TestForgotPassword(ApiTestCase):
    Q = "Name of my first school?"

    def setUp(self):
        super().setUp()
        coll_api._reset_clear_failures()

    def tearDown(self):
        coll_api._reset_clear_failures()
        super().tearDown()

    def _register(self, opener=None, **over):
        data = {"name": "dist", "password": "password1", "confirm_password": "password1",
                "secret_question": self.Q, "secret_answer": "St Mary's"}
        data.update(over)
        return self._post(opener or self._client(), "/register", data)

    def _setup_distributor(self):
        self._register()
        self._add_user("smA", "salesman", "pwA")

    def _start_reset(self, opener, username="dist"):
        return self._post(opener, "/forgot-password", {"username": username})

    def _do_reset(self, opener, answer="st mary's", pw="newpass1", confirm=None, username="dist"):
        return self._post(opener, "/forgot-password/reset", {
            "username": username, "secret_answer": answer,
            "new_password": pw, "confirm_password": pw if confirm is None else confirm})

    # ---- registration ------------------------------------------------------

    def test_register_requires_question_and_answer(self):
        for over in ({"secret_question": ""}, {"secret_answer": ""},
                     {"secret_question": "hey"}, {"secret_answer": "ab"}):
            status, body = self._register(**over)
            self.assertIn("alert-error", body, over)
            self.assertFalse(coll_store.has_any_users(), over)

    def test_register_error_keeps_typed_question(self):
        status, body = self._register(secret_answer="")
        self.assertIn(self.Q, body)

    def test_register_creates_distributor_with_question(self):
        status, body = self._register()
        self.assertIn("created", body)
        self.assertEqual(coll_store.get_distributor_secret_question(), self.Q)

    # ---- login-page link ---------------------------------------------------

    def test_forgot_link_hidden_before_setup_and_without_question(self):
        status, body = self._get(self._client(), "/login")
        self.assertNotIn("/forgot-password", body)
        self.assertIn("/register", body)
        coll_store.register_first_distributor("dist", "password1", "password1")  # no question
        status, body = self._get(self._client(), "/login")
        self.assertNotIn("/forgot-password", body)

    def test_forgot_link_shown_after_setup_with_question(self):
        self._setup_distributor()
        status, body = self._get(self._client(), "/login")
        self.assertIn("/forgot-password", body)
        self.assertNotIn("/register", body)

    def test_forgot_page_redirects_to_login_when_unavailable(self):
        status, body = self._get(self._client(), "/forgot-password")
        self.assertIn("Sign in", body)  # followed the redirect to /login
        self.assertNotIn("Reset the distributor password", body)

    # ---- two-step reset ----------------------------------------------------

    def test_full_reset_flow(self):
        self._setup_distributor()
        op = self._client()
        status, body = self._start_reset(op)
        self.assertIn(self.Q, body)
        status, body = self._do_reset(op)
        self.assertIn("Password reset", body)
        self.assertIsNotNone(coll_store.verify_user("dist", "newpass1"))
        self.assertIsNone(coll_store.verify_user("dist", "password1"))
        # New password logs in and lands on the menu without a forced change.
        status, body = self._get(self._login("dist", "newpass1"), "/menu")
        self.assertIn("Main Menu", body)

    def test_reset_invalidates_existing_sessions(self):
        self._setup_distributor()
        old = self._login("dist", "password1")
        self.assertIn("Main Menu", self._get(old, "/menu")[1])
        self._do_reset(self._client())
        status, body = self._get(old, "/menu")
        self.assertNotIn("Main Menu", body)

    def test_other_users_sessions_survive_a_reset(self):
        self._setup_distributor()
        sm = self._login("smA", "pwA")
        self._do_reset(self._client())
        self.assertIn("Main Menu", self._get(sm, "/menu")[1])

    def test_unknown_and_non_distributor_usernames_get_same_generic_message(self):
        self._setup_distributor()
        bodies = []
        for name in ("ghost", "smA"):
            status, body = self._start_reset(self._client(), name)
            self.assertIn("available for this account", body)
            self.assertNotIn(self.Q, body)
            bodies.append(re.sub(r'value="[^"]*"', "", body))
        self.assertEqual(bodies[0], bodies[1])

    def test_reset_step_rechecks_eligibility_server_side(self):
        self._setup_distributor()
        status, body = self._do_reset(self._client(), username="smA")
        self.assertIn("available for this account", body)
        self.assertIsNotNone(coll_store.verify_user("smA", "pwA"))

    def test_wrong_answer_and_bad_password_do_not_change_anything(self):
        self._setup_distributor()
        op = self._client()
        status, body = self._do_reset(op, answer="wrong")
        self.assertIn("That answer is incorrect", body)
        self.assertIn(self.Q, body)  # stays on step 2
        status, body = self._do_reset(op, pw="short")
        self.assertIn("alert-error", body)
        status, body = self._do_reset(op, pw="newpass1", confirm="different1")
        self.assertIn("alert-error", body)
        self.assertIsNotNone(coll_store.verify_user("dist", "password1"))

    def test_logged_in_user_is_sent_to_menu(self):
        self._setup_distributor()
        op = self._login("dist", "password1")
        status, body = self._get(op, "/forgot-password")
        self.assertIn("Main Menu", body)

    # ---- lockout -----------------------------------------------------------

    def test_five_wrong_answers_lock_the_flow_even_for_the_right_answer(self):
        self._setup_distributor()
        op = self._client()
        for i in range(4):
            status, body = self._do_reset(op, answer="wrong")
            self.assertIn("That answer is incorrect", body, i)
        status, body = self._do_reset(op, answer="wrong")
        self.assertIn("Too many incorrect attempts", body)
        status, body = self._do_reset(op, answer="st mary's")
        self.assertIn("Too many incorrect attempts", body)
        self.assertIsNotNone(coll_store.verify_user("dist", "password1"))
        # The lookup step is locked as well.
        status, body = self._start_reset(op)
        self.assertIn("Too many incorrect attempts", body)

    def test_lock_expires_after_fifteen_minutes(self):
        self._setup_distributor()
        op = self._client()
        for _ in range(5):
            self._do_reset(op, answer="wrong")
        coll_api._reset_state["locked_until"] = time.time() - 1
        status, body = self._do_reset(op)
        self.assertIn("Password reset", body)

    def test_each_successive_lock_is_longer_until_a_success(self):
        self._setup_distributor()
        op = self._client()
        expected = [15, 60, 24 * 60, 24 * 60]  # minutes; the last tier repeats
        for i, mins in enumerate(expected):
            for _ in range(5):
                self._do_reset(op, answer="wrong")
            self.assertAlmostEqual(coll_api._reset_locked_minutes(), mins, delta=1, msg=i)
            # Still locked against the right answer, then let it expire.
            status, body = self._do_reset(op, answer="st mary's")
            self.assertIn("Too many incorrect attempts", body)
            coll_api._reset_state["locked_until"] = time.time() - 1
        # A correct reset finally clears the escalation.
        status, body = self._do_reset(op)
        self.assertIn("Password reset", body)
        self.assertEqual(coll_api._reset_state["locks"], 0)

    def test_slow_drip_guessing_does_not_reset_the_counter(self):
        self._setup_distributor()
        op = self._client()
        for _ in range(5):
            self._do_reset(op, answer="wrong")
        coll_api._reset_state["locked_until"] = time.time() - 1  # first lock expires
        for _ in range(4):
            self._do_reset(op, answer="wrong")
        self.assertEqual(coll_api._reset_locked_minutes(), 0)  # 4 of 5: not yet locked
        self._do_reset(op, answer="wrong")
        self.assertGreater(coll_api._reset_locked_minutes(), 15)  # second lock is 1 h

    def test_long_lock_is_shown_in_hours(self):
        self.assertEqual(coll_api._format_wait(1), "1 minute")
        self.assertEqual(coll_api._format_wait(15), "15 minutes")
        self.assertEqual(coll_api._format_wait(1440), "24 hours")

    def test_success_clears_the_failure_counter(self):
        self._setup_distributor()
        op = self._client()
        for _ in range(4):
            self._do_reset(op, answer="wrong")
        self.assertEqual(coll_api._reset_state["failures"], 4)
        self._do_reset(op, pw="newpass1")
        self.assertEqual(coll_api._reset_state["failures"], 0)
        self.assertEqual(coll_api._reset_locked_minutes(), 0)

    # ---- Profile: set / change the question -------------------------------

    def _profile_post(self, op, **over):
        data = {"current_password": "password1", "secret_question": "Favourite colour?",
                "secret_answer": "Blue"}
        data.update(over)
        return self._post(op, "/profile/set-secret-question", data)

    def test_existing_distributor_can_set_question_from_profile(self):
        coll_store.register_first_distributor("dist", "password1", "password1")  # legacy: none
        op = self._login("dist", "password1")
        status, body = self._get(op, "/profile")
        self.assertIn("None set yet", body)
        status, body = self._profile_post(op)
        self.assertIn("Secret question saved", body)
        self.assertIn("Favourite colour?", body)
        self.assertNotIn("Blue", body)  # the answer is never echoed
        self.assertEqual(coll_store.get_distributor_secret_question(), "Favourite colour?")
        self.assertIn("/forgot-password", self._get(self._client(), "/login")[1])

    def test_profile_rejects_wrong_current_password(self):
        coll_store.register_first_distributor("dist", "password1", "password1")
        op = self._login("dist", "password1")
        status, body = self._profile_post(op, current_password="nope")
        self.assertIn("Current password is incorrect", body)
        self.assertIsNone(coll_store.get_distributor_secret_question())

    def test_profile_card_is_distributor_only_and_post_is_refused_for_others(self):
        self._setup_distributor()
        status, body = self._get(self._login("smA", "pwA"), "/profile")
        self.assertNotIn("Secret Question", body)
        status, body = self._profile_post(self._login("smA", "pwA"), current_password="pwA")
        self.assertNotIn("Secret question saved", body)
        self.assertEqual(coll_store.get_secret_question_for("smA"), None)
        self.assertEqual(coll_store.get_distributor_secret_question(), self.Q)
        status, body = self._get(self._login("dist", "password1"), "/profile")
        self.assertIn("Secret Question", body)
        self.assertIn(self.Q, body)


if __name__ == "__main__":
    unittest.main()
