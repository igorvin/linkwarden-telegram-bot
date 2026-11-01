import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import bot
from linkwarden import ApiError
from scan_jobs import ScanStore


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_STATE_PATH", str(tmp_path / "jobs.sqlite3"))
    return ScanStore()


def draft():
    return SimpleNamespace(chat_id=-1001872870761, thread_id=9, owner_id=100,
                           collection=1, chosen={2: "Tag"}, urls=("https://a.com", "https://b.com"),
                           message_id=55)


def context():
    return SimpleNamespace(bot_data={"scan_tasks": {}}, bot=SimpleNamespace(edit_message_text=AsyncMock()))


def test_job_create_deduplicates_and_preserves_checkpoint(state):
    first = state.create(draft())
    second = state.create(draft())
    assert first["id"] == second["id"]
    state.start(first["id"], 55)
    state.advance(first["id"], 0, True)
    state.advance(first["id"], 0, True)
    reopened = ScanStore().active(draft().chat_id, 9)
    assert reopened["cursor"] == 1 and reopened["created"] == 1
    state.state(first["id"], "cancelled")
    assert state.active(draft().chat_id, 9) is None
    assert state.create(draft())["id"] != first["id"]


@pytest.mark.asyncio
async def test_worker_progress_and_duplicates(state):
    job = state.create(draft())
    state.start(job["id"], 55)
    with patch("bot.save_once", side_effect=[(1, True), (2, False)]) as save, patch("bot.asyncio.sleep", new_callable=AsyncMock):
        await bot.run_scan(job["id"], context())
    result = state.get(job["id"])
    assert result["state"] == "done" and result["cursor"] == 2
    assert result["created"] == 1 and result["duplicates"] == 1
    assert save.call_count == 2


@pytest.mark.asyncio
async def test_worker_pauses_and_resumes_at_failed_url(state):
    job = state.create(draft())
    state.start(job["id"], 55)
    with patch("bot.save_once", side_effect=[(1, True), ApiError("HTTP 503")]), patch("bot.asyncio.sleep", new_callable=AsyncMock):
        await bot.run_scan(job["id"], context())
    assert state.get(job["id"])["state"] == "paused"
    assert state.get(job["id"])["cursor"] == 1
    state.start(job["id"], 56)
    with patch("bot.save_once", return_value=(2, True)) as save, patch("bot.asyncio.sleep", new_callable=AsyncMock):
        await bot.run_scan(job["id"], context())
    save.assert_called_once_with("https://b.com", 1, {2: "Tag"})
    assert state.get(job["id"])["state"] == "done"


@pytest.mark.asyncio
async def test_cancel_during_post_stops_next_item(state):
    job = state.create(draft())
    state.start(job["id"], 55)
    def cancel_then_finish(*args):
        state.state(job["id"], "cancelled")
        return 1, True
    with patch("bot.save_once", side_effect=cancel_then_finish) as save, patch("bot.asyncio.sleep", new_callable=AsyncMock):
        await bot.run_scan(job["id"], context())
    save.assert_called_once()
    assert state.get(job["id"])["state"] == "cancelled"
    assert state.get(job["id"])["cursor"] == 1


@pytest.mark.asyncio
async def test_worker_shutdown_keeps_job_resumable(state):
    job = state.create(draft())
    state.start(job["id"], 55)
    with patch("bot.asyncio.to_thread", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await bot.run_scan(job["id"], context())
    assert state.get(job["id"])["state"] == "paused"
    assert state.get(job["id"])["cursor"] == 0
