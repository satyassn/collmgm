"""
Unit tests for scripts/coll_data.py validators.

Run:  python -m unittest discover -s tests -v
"""

import sys
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import coll_data


VALID_BEATS = {"beat1"}
VALID_SALESMEN = {"sm1"}
TODAY = date.today().isoformat()
TOMORROW = (date.today() + timedelta(days=1)).isoformat()


def _validate(bill_no="INV1", date_str=TODAY, amount_str="100.00",
              beat="beat1", salesman="sm1", existing=frozenset()):
    return coll_data.validate_single_voucher(
        bill_no, date_str, amount_str, beat, salesman,
        existing, VALID_BEATS, VALID_SALESMEN)


class TestValidateSingleVoucher(unittest.TestCase):
    def test_valid_voucher_passes(self):
        errors, amount = _validate()
        self.assertEqual(errors, [])
        self.assertEqual(amount, Decimal("100.00"))

    def test_rejects_non_finite_amounts(self):
        for bad in ("NaN", "Infinity", "-Infinity"):
            errors, amount = _validate(amount_str=bad)
            self.assertTrue(any("positive number" in e for e in errors), bad)
            self.assertIsNone(amount, bad)

    def test_rejects_zero_and_negative_amounts(self):
        for bad in ("0", "-5"):
            errors, amount = _validate(amount_str=bad)
            self.assertTrue(any("positive number" in e for e in errors), bad)
            self.assertIsNone(amount, bad)

    def test_rejects_bill_no_bad_characters(self):
        for bad in ("a,b", 'a"b', "=SUM(A1)", "+1", "-1", "@cmd", "a b", "a\tb"):
            errors, _ = _validate(bill_no=bad)
            self.assertTrue(any("invalid characters" in e for e in errors), bad)

    def test_accepts_bill_no_allowed_characters(self):
        for good in ("INV/2026-01.A", "123", "a_b-c.d/e"):
            errors, _ = _validate(bill_no=good)
            self.assertEqual(errors, [], good)

    def test_rejects_future_date(self):
        errors, _ = _validate(date_str=TOMORROW)
        self.assertTrue(any("in the future" in e for e in errors))

    def test_accepts_today(self):
        errors, _ = _validate(date_str=TODAY)
        self.assertEqual(errors, [])

    def test_rejects_malformed_date(self):
        errors, _ = _validate(date_str="20260101")
        self.assertTrue(any("Invalid date" in e for e in errors))


class TestValidateAddvBatch(unittest.TestCase):
    def _batch(self, voucher_rows, inst_rows):
        return coll_data.validate_addv_batch(
            voucher_rows, inst_rows, set(), VALID_BEATS, VALID_SALESMEN,
            "tester", "2026-01-01T00:00:00")

    def _voucher_row(self, **overrides):
        row = {"bill_no": "INV1", "date": TODAY, "amount": "100.00",
               "beat": "beat1", "salesman": "sm1"}
        row.update(overrides)
        return row

    def test_valid_batch_passes(self):
        errors, vouchers, installments = self._batch(
            [self._voucher_row()],
            [{"bill_no": "INV1", "date": TODAY, "amount": "40", "salesman": "sm1"}])
        self.assertEqual(errors, [])
        self.assertEqual(vouchers[0]["balance"], "60.00")
        self.assertEqual(len(installments), 1)

    def test_rejects_non_finite_installment_amount(self):
        for bad in ("NaN", "Infinity"):
            errors, _, _ = self._batch(
                [self._voucher_row()],
                [{"bill_no": "INV1", "date": TODAY, "amount": bad, "salesman": "sm1"}])
            self.assertTrue(any("positive number" in e for e in errors), bad)

    def test_rejects_future_installment_date(self):
        errors, _, _ = self._batch(
            [self._voucher_row()],
            [{"bill_no": "INV1", "date": TOMORROW, "amount": "10", "salesman": "sm1"}])
        self.assertTrue(any("in the future" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
