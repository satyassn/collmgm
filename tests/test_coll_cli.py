"""
Unit tests for scripts/coll_cli.py input handling.

Run:  python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import coll_cli


def _feed(*values):
    """Patch builtins.input to return the given values in order."""
    return patch("builtins.input", side_effect=list(values))


class TestReadInput(unittest.TestCase):
    def test_returns_typed_value(self):
        with _feed("hello"):
            self.assertEqual(coll_cli.read_input("? "), "hello")

    def test_eof_raises_input_cancelled(self):
        with patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(coll_cli.InputCancelled):
                coll_cli.read_input("? ")

    def test_keyboard_interrupt_raises_input_cancelled(self):
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            with self.assertRaises(coll_cli.InputCancelled):
                coll_cli.read_input("? ")


class TestGetPaymentInput(unittest.TestCase):
    def _voucher(self, balance="50.00", payment=""):
        return {"bill_no": "B001", "balance": balance, "payment": payment}

    def _get(self, voucher, *inputs):
        with _feed(*inputs):
            return coll_cli.get_payment_input(voucher, 0, 1, "0")

    def test_valid_amount_accepted(self):
        payment, action = self._get(self._voucher(), "10")
        self.assertEqual(payment, "10")
        self.assertEqual(action, "next")

    def test_non_finite_amounts_rejected(self):
        # nan/inf must be re-prompted, not stored; 'n' then exits the loop.
        for bad in ("nan", "inf", "-inf", "infinity"):
            payment, action = self._get(self._voucher(payment="5.00"), bad, "n")
            self.assertEqual(payment, "5.00", bad)
            self.assertEqual(action, "next", bad)

    def test_over_balance_rejected(self):
        payment, action = self._get(self._voucher(balance="50.00"), "50.01", "n")
        self.assertEqual(payment, "")

    def test_corrupt_stored_balance_limits_entry_to_zero(self):
        # Any positive amount is refused (balance treated as 0); 0 is allowed.
        payment, action = self._get(self._voucher(balance="garbage"), "10", "0")
        self.assertEqual(payment, "0")
        self.assertEqual(action, "next")


class TestSelectFromList(unittest.TestCase):
    ITEMS = ["a", "b", "c"]

    def test_huge_range_rejected_without_expansion(self):
        with _feed("1-999999999", "b"):
            result = coll_cli.select_from_list(self.ITEMS, "item", allow_multiple=True)
        self.assertIsNone(result)

    def test_valid_range_and_list_still_work(self):
        with _feed("1,3"):
            result = coll_cli.select_from_list(self.ITEMS, "item", allow_multiple=True)
        self.assertEqual(result, ["a", "c"])
        with _feed("1-2"):
            result = coll_cli.select_from_list(self.ITEMS, "item", allow_multiple=True)
        self.assertEqual(result, ["a", "b"])

    def test_zero_and_reversed_ranges_rejected(self):
        with _feed("0-2", "3-1", "b"):
            result = coll_cli.select_from_list(self.ITEMS, "item", allow_multiple=True)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
