"""
ONE-48 — BOI parser acceptance test spec (T1-T11).

This file is the ACCEPTANCE SPEC for the BOI handler re-evaluation. Agents
(qwen-coder-next and gemma-4-31b-it) must produce a handler that makes every
test in this file pass on `BOI.pdf`.

Place handler at:   PosPythonBackend/app/util/boi_bank_handler.py
Place this file at: PosPythonBackend/tests/util/test_boi_bank_handler.py
Golden fixture at:  PosPythonBackend/tests/fixtures/boi_expected.json

The handler must expose `parse(pdf_path: str) -> dict` returning:
  {
    "metadata": {
      "bank_name", "branch", "account_name", "account_number",
      "customer_id", "account_type", "ifsc_code",
      "statement_period_start", "statement_period_end",
      "statement_generated_at"
    },
    "transactions": [
      {
        "serial", "txn_date", "description", "chequeNo",
        "transactionAmount", "transactionDirection", "balance"
      }, ...
    ]
  }
"""
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from app.util.boi_bank_handler import parse

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"
PDF_PATH = str(FIXTURE_DIR / "BOI.pdf")
EXPECTED = json.loads((FIXTURE_DIR / "boi_expected.json").read_text())


@pytest.fixture(scope="module")
def parsed():
    return parse(PDF_PATH)


# ---------- Metadata ----------

def test_T1_account_number_extracted(parsed):
    """T1: account_number is non-null string matching BOI format."""
    acc = parsed["metadata"]["account_number"]
    assert acc is not None
    assert isinstance(acc, str)
    assert re.match(r"^\d{10,18}$", acc), f"bad account_number: {acc!r}"
    assert acc == EXPECTED["metadata"]["account_number"]


def test_T2_account_name_extracted(parsed):
    """T2: account_name is non-null, length > 3, matches holder."""
    name = parsed["metadata"]["account_name"]
    assert name is not None
    assert isinstance(name, str) and len(name) > 3
    assert name == EXPECTED["metadata"]["account_name"]


def test_T2b_other_metadata(parsed):
    """T2b: bank_name, branch, ifsc, customer_id, account_type present."""
    md = parsed["metadata"]
    assert md["bank_name"] == "Bank of India"
    assert md["branch"] == EXPECTED["metadata"]["branch"]
    assert md["ifsc_code"] == EXPECTED["metadata"]["ifsc_code"]
    assert md["customer_id"] == EXPECTED["metadata"]["customer_id"]
    assert md["account_type"] == EXPECTED["metadata"]["account_type"]


# ---------- Direction inference ----------

def test_T3_direction_inferred_from_column(parsed):
    """T3: direction must be inferred from Withdrawal vs Deposits column.

    Sample contains only deposits → all 12 must be CR. Hardcoded 'CR'
    would also pass here, so T3b enforces inference logic via code-inspection.
    """
    txns = parsed["transactions"]
    assert len(txns) == 12
    for t in txns:
        assert t["transactionDirection"] in ("CR", "DR")
    # All 12 rows in this sample are deposits.
    assert all(t["transactionDirection"] == "CR" for t in txns)


def test_T3b_direction_not_hardcoded():
    """T3b: handler source must not contain a constant 'CR' assignment
    that ignores the column. Must show evidence of reading Withdrawal
    vs Deposits columns (i.e. conditional).
    """
    src = Path("app/util/boi_bank_handler.py").read_text()
    lower = src.lower()
    assert "withdrawal" in lower, "handler must reference Withdrawal column"
    assert "deposit" in lower, "handler must reference Deposits column"
    # Forbid obvious hardcoding patterns
    assert not re.search(r'["\']direction["\']\s*:\s*["\']CR["\']', src, re.IGNORECASE), \
        "direction must not be hardcoded to CR"
    assert not re.search(r'transactionDirection\s*=\s*["\']CR["\']', src), \
        "transactionDirection must not be assigned the literal 'CR'"


# ---------- Descriptions ----------

def test_T4_descriptions_match_golden(parsed):
    """T4: every description equals golden string (no truncation, no extra)."""
    for got, want in zip(parsed["transactions"], EXPECTED["transactions"]):
        assert got["description"] == want["description"], (
            f"row {want['serial']}: got {got['description']!r} "
            f"want {want['description']!r}"
        )


def test_T5_descriptions_not_mid_word_truncated(parsed):
    """T5: no description ends in an orphan partial token.

    Heuristic: descriptions should not end with '/' or a lone letter
    followed by no terminator, and should not drop the trailing word that
    the previous PDF-extractor broke. Golden-file match (T4) is authoritative;
    this test catches regressions when golden is loosened.
    """
    for t in parsed["transactions"]:
        desc = t["description"]
        assert not desc.endswith("/"), f"desc ends with stray '/': {desc!r}"
        assert not re.search(r"\s[A-Z]{1,2}$", desc) or desc.endswith("PRIV") or desc.endswith("CARRIE"), \
            f"desc looks mid-word truncated: {desc!r}"


# ---------- Cheque numbers ----------

def test_T6_cheque_no_null_when_blank(parsed):
    """T6: sample has Cheque No column blank on every row — all must be null.
    Must NOT leak description tokens into chequeNo.
    """
    for t in parsed["transactions"]:
        assert t["chequeNo"] is None, (
            f"row {t['serial']}: chequeNo should be None, got {t['chequeNo']!r}"
        )


# ---------- Amounts + balances ----------

def test_T7_amounts_match_golden(parsed):
    """T7: all 12 transactionAmount values match golden (indian-format parsing)."""
    for got, want in zip(parsed["transactions"], EXPECTED["transactions"]):
        assert Decimal(str(got["transactionAmount"])) == Decimal(str(want["transactionAmount"])), \
            f"row {want['serial']}: amount mismatch {got['transactionAmount']} vs {want['transactionAmount']}"


def test_T8_balances_match_golden(parsed):
    """T8: all 12 balance values match golden (including negative cash-credit)."""
    for got, want in zip(parsed["transactions"], EXPECTED["transactions"]):
        assert Decimal(str(got["balance"])) == Decimal(str(want["balance"])), \
            f"row {want['serial']}: balance mismatch {got['balance']} vs {want['balance']}"


# ---------- Ledger sanity ----------

def test_T9_ledger_sanity(parsed):
    """T9: sum(CR) - sum(DR) == closing_balance - opening_balance.

    opening = row1_balance - row1_signed_amount
    closing = last row balance
    """
    txns = parsed["transactions"]
    first = txns[0]
    sign0 = Decimal("1") if first["transactionDirection"] == "CR" else Decimal("-1")
    opening = Decimal(str(first["balance"])) - sign0 * Decimal(str(first["transactionAmount"]))
    closing = Decimal(str(txns[-1]["balance"]))
    total = sum(
        (Decimal("1") if t["transactionDirection"] == "CR" else Decimal("-1"))
        * Decimal(str(t["transactionAmount"]))
        for t in txns
    )
    assert closing - opening == total, (
        f"ledger mismatch: delta={closing - opening} sum={total}"
    )


# ---------- Row count + golden deep-equal ----------

def test_T10_row_count(parsed):
    """T10: exactly 12 transactions."""
    assert len(parsed["transactions"]) == 12


def test_T11_golden_deep_equal(parsed):
    """T11: full parsed output deep-equals golden fixture (metadata + txns)."""
    # Metadata
    for k, v in EXPECTED["metadata"].items():
        assert parsed["metadata"].get(k) == v, f"metadata[{k}] mismatch"
    # Transactions field-by-field
    for got, want in zip(parsed["transactions"], EXPECTED["transactions"]):
        for field in ("serial", "txn_date", "description", "chequeNo",
                      "transactionDirection"):
            assert got[field] == want[field], (
                f"row {want['serial']} field {field}: {got[field]!r} vs {want[field]!r}"
            )
        assert Decimal(str(got["transactionAmount"])) == Decimal(str(want["transactionAmount"]))
        assert Decimal(str(got["balance"])) == Decimal(str(want["balance"]))
