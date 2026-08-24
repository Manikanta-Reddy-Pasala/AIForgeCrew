"""Unit tests for the slim PDS-delegate ``trial_balance`` module.

Real PDS isn't running in unit-test land, so HTTP is patched with
``unittest.mock`` and the focus is on correct request shaping +
response handling. Integration coverage of PDS itself lives in the
PosDataSyncService repo.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aiforge_core.memory import trial_balance as tb


def _xlsx(tmp_path: Path, name: str) -> Path:
    """Tiny stub file — the unit test never reads back its content,
    we just need a valid filesystem path for the open() call."""
    p = tmp_path / name
    p.write_bytes(b"PK\x03\x04stub")
    return p


# ─── render_markdown / _has_material_gap ─────────────────────────────


def test_render_markdown_summary_and_buckets() -> None:
    md = tb.render_markdown({
        "summary": {"tallyTotal": 100, "oneshellTotal": 95, "gap": 5},
        "buckets": {
            "matched": [{"name": "Cash"}, {"name": "Bank"}],
            "large": [{"name": "Sundry Debtors", "delta": 12000}],
        },
    })
    assert "tallyTotal" in md
    assert "100" in md
    assert "## Buckets" in md
    assert "matched (2)" in md
    assert "large (1)" in md
    assert "Sundry Debtors" in md


def test_render_markdown_unexpected_schema_dumps_raw() -> None:
    md = tb.render_markdown({"weird_key": [1, 2, 3]})
    assert "Raw PDS response" in md
    assert "weird_key" in md


def test_has_material_gap_via_buckets() -> None:
    assert tb._has_material_gap({"buckets": {"large": [{"x": 1}]}}) is True
    assert tb._has_material_gap({"buckets": {"mismatchedLeaves": [{}]}}) is True
    assert tb._has_material_gap({"buckets": {"matched": [{}]}}) is False


def test_has_material_gap_via_summary_flag() -> None:
    assert tb._has_material_gap({"summary": {"hasMaterialGap": True}}) is True
    assert tb._has_material_gap({"summary": {"hasMaterialGap": False}}) is False


def test_has_material_gap_empty() -> None:
    assert tb._has_material_gap({}) is False


# ─── call_pds_compare HTTP shaping ───────────────────────────────────


def test_call_pds_compare_posts_multipart(tmp_path: Path) -> None:
    tally = _xlsx(tmp_path, "tally.xlsx")
    one = _xlsx(tmp_path, "oneshell.xlsx")

    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {"summary": {"gap": 0}}

    with patch("httpx.post", return_value=fake_resp) as post_mock:
        result = tb.call_pds_compare(tally, one, business_id="b1234")
    assert result == {"summary": {"gap": 0}}

    # Request shape: correct URL, correct files dict keys, biz id header.
    args, kwargs = post_mock.call_args
    assert args[0].endswith("/compare-tb-files")
    files = kwargs["files"]
    assert "tallyTb" in files
    assert "oneshellTb" in files
    headers = kwargs["headers"]
    assert headers.get("X-Business-Id") == "b1234"


def test_call_pds_compare_omits_header_when_no_biz(tmp_path: Path) -> None:
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {}
    with patch("httpx.post", return_value=fake_resp) as post_mock:
        tb.call_pds_compare(_xlsx(tmp_path, "t"), _xlsx(tmp_path, "o"))
    headers = post_mock.call_args.kwargs["headers"]
    assert "X-Business-Id" not in headers


def test_call_pds_compare_raises_on_non_2xx(tmp_path: Path) -> None:
    fake_resp = MagicMock(status_code=500, text="boom")
    with patch("httpx.post", return_value=fake_resp):
        with pytest.raises(OSError, match="500"):
            tb.call_pds_compare(_xlsx(tmp_path, "t"), _xlsx(tmp_path, "o"))


def test_call_pds_compare_wraps_transport_error(tmp_path: Path) -> None:
    import httpx
    with patch("httpx.post",
               side_effect=httpx.ConnectError("refused")):
        with pytest.raises(OSError, match="PDS unreachable"):
            tb.call_pds_compare(_xlsx(tmp_path, "t"), _xlsx(tmp_path, "o"))


def test_call_pds_compare_honours_env_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AIFORGE_PDS_API_BASE",
        "http://nuc.local:8092/api/v1/data/tally-ingest",
    )
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {}
    with patch("httpx.post", return_value=fake_resp) as post_mock:
        tb.call_pds_compare(_xlsx(tmp_path, "t"), _xlsx(tmp_path, "o"))
    assert post_mock.call_args[0][0].startswith("http://nuc.local:8092/")


# ─── run_workflow ─────────────────────────────────────────────────────


def test_run_workflow_blocks_on_missing_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "aiforge_core.memory.online_learner.attachments_for",
        lambda _id: [],
    )
    out = tb.run_workflow({"identifier": "ONE-1", "title": "tb", "body": ""})
    assert out["blocked_by_detectors"] is True
    assert out["problems"][0]["mode"] == "missing_attachment"


def test_run_workflow_blocks_on_pds_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tally = _xlsx(tmp_path, "t")
    one = _xlsx(tmp_path, "o")
    monkeypatch.setattr(
        "aiforge_core.memory.online_learner.attachments_for",
        lambda _id: [
            {"role": "tally", "file_path": str(tally)},
            {"role": "oneshell", "file_path": str(one)},
        ],
    )
    import httpx
    with patch("httpx.post", side_effect=httpx.ConnectError("nope")):
        out = tb.run_workflow({"identifier": "ONE-2", "title": "", "body": ""})
    assert out["blocked_by_detectors"] is True
    assert out["problems"][0]["mode"] == "pds_unreachable"


def test_run_workflow_returns_doer_outcome_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tally = _xlsx(tmp_path, "t")
    one = _xlsx(tmp_path, "o")
    monkeypatch.setattr(
        "aiforge_core.memory.online_learner.attachments_for",
        lambda _id: [
            {"role": "tally", "file_path": str(tally)},
            {"role": "oneshell", "file_path": str(one)},
        ],
    )
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {
        "summary": {"gap": 0, "hasMaterialGap": False},
        "buckets": {"matched": [{"name": "Cash"}]},
    }
    with patch("httpx.post", return_value=fake_resp):
        out = tb.run_workflow({
            "identifier": "ONE-3",
            "title": "trial balance acme",
            "body": "for b117695104178401 in qa",
        })
    assert out["artifact_type"] == "doer_outcome"
    assert out["mode"] == "pds-delegate"
    assert out["blocked_by_detectors"] is False
    assert out["business_id"] == "b117695104178401"
    assert "Tally" in out["udiff"]
    assert out["raw"]["summary"]["gap"] == 0


def test_run_workflow_blocks_on_material_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tally = _xlsx(tmp_path, "t")
    one = _xlsx(tmp_path, "o")
    monkeypatch.setattr(
        "aiforge_core.memory.online_learner.attachments_for",
        lambda _id: [
            {"role": "tally", "file_path": str(tally)},
            {"role": "oneshell", "file_path": str(one)},
        ],
    )
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {
        "summary": {"gap": 12000},
        "buckets": {"large": [{"name": "Sundry Debtors"}]},
    }
    with patch("httpx.post", return_value=fake_resp):
        out = tb.run_workflow({"identifier": "ONE-4", "title": "tb", "body": ""})
    assert out["blocked_by_detectors"] is True
