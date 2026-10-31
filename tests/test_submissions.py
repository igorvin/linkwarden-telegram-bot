from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from linkwarden import ApiError
from submissions import SubmissionLedger, save_once


@pytest.fixture
def ledger_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_STATE_PATH", str(tmp_path / "state" / "submissions.sqlite3"))
    monkeypatch.setenv("LINKWARDEN_API_URL", "https://linkwarden.example.com")
    monkeypatch.setenv("LINKWARDEN_API_KEY", "test-key")


def test_claim_is_atomic_across_connections(ledger_env):
    ledger = SubmissionLedger()
    key = ledger.key("https://example.com")
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: ledger.claim(key)[0], range(8)))
    assert sum(claims) == 1


def test_second_send_and_restart_do_not_post_again(ledger_env):
    with patch("submissions.find_link", return_value=None), patch("submissions.save_link", return_value=21) as post:
        assert save_once("https://example.com", 1, {}) == (21, True)
        assert save_once("https://example.com", 2, {7: "Other"}) == (21, False)
        post.assert_called_once()


def test_existing_link_is_not_recreated(ledger_env):
    with patch("submissions.find_link", return_value=22), patch("submissions.save_link") as post:
        assert save_once("https://example.com", 1, {}) == (22, False)
        post.assert_not_called()


def test_timeout_is_durable_and_can_be_reconciled(ledger_env):
    with patch("submissions.find_link", return_value=None), patch("submissions.save_link", side_effect=ApiError("Timeout", True)) as post:
        with pytest.raises(ApiError):
            save_once("https://example.com", 1, {})
        with pytest.raises(ApiError):
            save_once("https://example.com", 1, {})
        post.assert_called_once()
    with patch("submissions.find_link", return_value=23), patch("submissions.save_link") as post:
        assert save_once("https://example.com", 1, {}) == (23, False)
        post.assert_not_called()


def test_get_failure_releases_claim_before_post(ledger_env):
    with patch("submissions.find_link", side_effect=ApiError("HTTP 503")), patch("submissions.save_link") as post:
        with pytest.raises(ApiError):
            save_once("https://example.com", 1, {})
        post.assert_not_called()
    ledger = SubmissionLedger()
    assert ledger.claim(ledger.key("https://example.com"))[0]


def test_definite_post_rejection_allows_later_attempt(ledger_env):
    with patch("submissions.find_link", return_value=None), patch("submissions.save_link", side_effect=ApiError("HTTP 403")):
        with pytest.raises(ApiError):
            save_once("https://example.com", 1, {})
    with patch("submissions.find_link", return_value=None), patch("submissions.save_link", return_value=24):
        assert save_once("https://example.com", 1, {}) == (24, True)


def test_pending_claim_after_crash_does_not_post(ledger_env):
    ledger = SubmissionLedger()
    ledger.claim(ledger.key("https://example.com"))
    with patch("submissions.find_link", return_value=None), patch("submissions.save_link") as post:
        with pytest.raises(ApiError):
            save_once("https://example.com", 1, {})
        post.assert_not_called()


def test_disk_error_prevents_post(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_STATE_PATH", str(tmp_path))
    with patch("submissions.save_link") as post:
        with pytest.raises(Exception):
            save_once("https://example.com", 1, {})
        post.assert_not_called()

def test_bindings_are_durable_scoped_and_reassignable(ledger_env):
    from submissions import get_binding, set_binding
    ledger = SubmissionLedger()
    assert get_binding(-100, 9) is None
    set_binding(-100, 9, 1)
    set_binding(-100, 42, 2)
    set_binding(123, None, 3)
    assert get_binding(-100, 9) == 1
    assert get_binding(-100, 42) == 2
    assert get_binding(123, None) == 3
    assert get_binding(456, None) is None
    set_binding(-100, 9, 4)
    assert get_binding(-100, 9) == 4
    assert get_binding(-100, 42) == 2


def test_binding_migration_preserves_previous_journal(ledger_env):
    import sqlite3
    import os
    from pathlib import Path
    from submissions import get_binding, set_binding
    path = Path(os.environ["BOT_STATE_PATH"])
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE submissions (fingerprint TEXT PRIMARY KEY, state TEXT NOT NULL, link_id INTEGER)")
        db.execute("INSERT INTO submissions VALUES ('existing', 'saved', 77)")
    set_binding(-100, 9, 1)
    ledger = SubmissionLedger()
    assert ledger.claim("existing") == (False, "saved", 77)
    assert get_binding(-100, 9) == 1
