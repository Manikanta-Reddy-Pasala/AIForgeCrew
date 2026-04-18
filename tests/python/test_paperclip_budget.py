from __future__ import annotations

from pathlib import Path

import pytest

from aiforge_core.budget import BudgetExceeded, Spend, assert_within_budget, record, ticket_tokens
from aiforge_core.config import PaperclipConfig
from aiforge_core.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ticket_token_cap(tmp_path: Path) -> None:
    s = Store(tmp_path / "db.sqlite")
    cfg = PaperclipConfig.load(REPO_ROOT)
    t = s.create_ticket("t", "", assignee="em")

    # cap for sr_developer = 150_000 per paperclip.config.yml
    record(s, t.id, Spend(role="sr_developer", tokens=100_000))
    assert ticket_tokens(s, t.id, "sr_developer") == 100_000

    # one more 40k ok (total 140k)
    assert_within_budget(cfg, s, t.id, "sr_developer", Spend(role="sr_developer", tokens=40_000))

    # another 20k exceeds (total would be 160k > 150k) — must raise
    with pytest.raises(BudgetExceeded):
        assert_within_budget(cfg, s, t.id, "sr_developer", Spend(role="sr_developer", tokens=60_000))


def test_role_without_budget_allows(tmp_path: Path) -> None:
    s = Store(tmp_path / "db.sqlite")
    cfg = PaperclipConfig.load(REPO_ROOT)
    t = s.create_ticket("t", "", assignee="em")
    assert_within_budget(cfg, s, t.id, "human", Spend(role="human", tokens=999_999_999))  # no-op
