import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from codex_telegram_agent.codex import AppServer
from codex_telegram_agent.db import Database
from codex_telegram_agent.worker import Worker


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "jobs.sqlite3")
    yield database
    database.db.close()


def test_duplicate_submission_cannot_run_twice(db):
    first = db.submit("update:1", "first task")
    assert db.submit("update:1", "same update with different content") == first
    assert db.next() == (first, "first task")
    assert db.next() is None
    db.recover()
    assert db.next() is None
    assert (
        db.db.execute("SELECT status FROM jobs WHERE id=?", (first,)).fetchone()[0] == "interrupted"
    )
    assert "не повторюється" in db.poll()[0]["text"]


def test_delivery_ids_are_not_reused_after_ack(db):
    db.emit("message one")
    first = db.poll()[0]
    db.ack([first["id"]])
    db.emit("message two")
    assert db.poll()[0]["id"] > first["id"]


def test_queue_limit_stop_and_new_dialog(db):
    worker = Worker(db, AsyncMock())
    for number in range(5):
        worker.dispatch({"op": "submit", "request_id": str(number), "text": "task"})
    with pytest.raises(ValueError, match="queue_full"):
        worker.dispatch({"op": "submit", "request_id": "six", "text": "task"})
    with pytest.raises(ValueError, match="busy"):
        worker.dispatch({"op": "new"})
    db.set("thread", "old")
    worker.dispatch({"op": "stop"})
    assert worker.cancel.is_set()
    assert db.next() is None
    worker.dispatch({"op": "new"})
    assert db.get("thread") == ""


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"op": "shell", "command": "anything"},
        {"op": "submit", "text": "task"},
        {"op": "submit", "text": "x" * 12_001, "request_id": "1"},
        {"op": "ack", "ids": [True]},
        {"op": "ack", "ids": "1"},
    ],
)
def test_ipc_input_fails_closed(db, payload):
    with pytest.raises(ValueError):
        Worker(db, AsyncMock()).dispatch(payload)


def test_long_response_is_split_and_durable(db):
    body = "ї" * 7000
    db.emit(body)
    messages = db.poll()
    assert "".join(item["text"] for item in messages) == body
    assert all(len(item["text"]) <= 3000 for item in messages)


def test_app_server_uses_profile_for_new_and_resumed_threads(db):
    class FakeApp(AppServer):
        def __init__(self):
            super().__init__(Path("/fake/state"), Path("/fake/projects"))
            self.calls = []

        async def start(self):
            pass

        async def request(self, method, params):
            self.calls.append((method, params))
            if method in {"thread/start", "thread/resume"}:
                return {"thread": {"id": "thread-test"}}
            if method == "turn/start":
                await self.events.put(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-test",
                            "turnId": "turn-test",
                            "item": {"type": "agentMessage", "text": "готово"},
                        },
                    }
                )
                await self.events.put(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-test",
                            "turn": {"id": "turn-test", "status": "completed"},
                        },
                    }
                )
                return {"turn": {"id": "turn-test"}}

    async def run():
        app = FakeApp()
        assert await app.run("перше завдання", db, asyncio.Event()) == "completed"
        assert await app.run("продовження", db, asyncio.Event()) == "completed"
        for method, params in app.calls:
            assert params["permissions"] == "telegram"
            assert params["approvalPolicy"] == "never"
            if method == "turn/start":
                assert params["effort"] == "high"
            else:
                assert params["model"] == "gpt-6-luna"
        assert [method for method, _ in app.calls] == [
            "thread/start",
            "turn/start",
            "thread/resume",
            "turn/start",
        ]
        assert [item["text"] for item in db.poll()] == ["готово", "готово"]

    asyncio.run(run())


def test_permission_requests_are_denied_without_leaking_tool_results():
    class Process:
        stdout = None

    async def run():
        app = AppServer(Path("/fake/state"), Path("/fake/projects"))
        app.process = Process()
        app.process.stdout = asyncio.StreamReader()
        app.send = AsyncMock()
        app.process.stdout.feed_data(
            json.dumps(
                {
                    "id": 4,
                    "method": "item/permissions/requestApproval",
                    "params": {"reason": "escalate"},
                }
            ).encode()
            + b"\n"
        )
        app.process.stdout.feed_eof()
        await app.read()
        reply = app.send.call_args.args[0]
        assert reply["id"] == 4
        assert "error" in reply
        assert "result" not in reply

    asyncio.run(run())


def test_old_conversation_explains_new_instead_of_blanket_sudo_refusal(db):
    class Probe(AppServer):
        async def start(self):
            pass

        async def request(self, method, params):
            if method == "thread/resume":
                assert params["threadId"] == "existing"
                assert "root-інструмент ще НЕ зареєстрований" in params["developerInstructions"]
                assert "/new" in params["developerInstructions"]
                assert "а не підстава відмовити" in params["developerInstructions"]
                return {"thread": {"id": "existing"}}
            await self.events.put(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "existing",
                        "turn": {"id": "turn", "status": "completed"},
                    },
                }
            )
            return {"turn": {"id": "turn"}}

    async def run():
        db.set("thread", "existing")
        app = Probe(Path("/fake/state"), Path("/fake/projects"), root=AsyncMock())
        assert await app.run("Запусти id -u через sudo", db, asyncio.Event()) == "completed"
        assert db.get("thread") == "existing"

    asyncio.run(run())


def test_router_isolates_threads_queues_messages_and_root_codes(tmp_path):
    from unittest.mock import Mock

    from codex_telegram_agent.worker import Router

    workers = {}
    for user in (100, 200):
        db = Database(tmp_path / f"{user}.sqlite3")
        root = Mock()
        root.check.return_value = {"ok": True, "id": str(user)}
        workers[user] = Worker(db, AsyncMock(), root)
        db.set("thread", f"thread-{user}")
        db.emit(f"private-{user}")
    router = Router(workers)
    router.dispatch({"user_id": 200, "op": "new"})
    assert workers[100].db.get("thread") == "thread-100"
    assert workers[200].db.get("thread") == ""
    router.dispatch({"user_id": 100, "op": "submit", "text": "owner", "request_id": "1"})
    router.dispatch({"user_id": 200, "op": "stop"})
    assert workers[100].db.next() is not None
    assert not workers[100].cancel.is_set()
    assert workers[200].cancel.is_set()
    assert router.dispatch({"user_id": 200, "op": "poll"})["messages"][0]["text"] == "private-200"
    router.dispatch({"user_id": 200, "op": "ack", "ids": [1]})
    assert workers[100].db.poll()[0]["text"] == "private-100"
    assert workers[200].db.poll() == []
    assert router.dispatch({"user_id": 200, "op": "root_check", "code": "CODE"})["id"] == "200"
    workers[100].root.check.assert_not_called()
    for user in (None, True, 300, "200"):
        with pytest.raises(ValueError):
            router.dispatch({"user_id": user, "op": "poll"})
    for worker in workers.values():
        worker.db.db.close()


def test_router_refresh_adds_users_and_revokes_existing_controls(tmp_path):
    from unittest.mock import Mock

    from codex_telegram_agent.worker import Router

    path = tmp_path / "access.json"
    created = {}

    def configure(owner, extended):
        path.write_text(json.dumps({"owner_id": owner, "extended_user_ids": extended}))

    def factory(user):
        database = Database(tmp_path / f"user-{user}.sqlite3")
        database.set("thread", f"thread-{user}")
        worker = Worker(database, AsyncMock(), Mock())
        created[user] = worker
        return worker

    configure(200, [])
    router = Router({}, path, factory)
    assert router.dispatch({"user_id": 200, "op": "health"})["ok"]
    with pytest.raises(ValueError):
        router.dispatch({"user_id": 100, "op": "health"})
    configure(100, [200])
    assert router.dispatch({"user_id": 100, "op": "health"})["ok"]
    assert created[100].db.get("thread") == "thread-100"
    assert created[200].db.get("thread") == "thread-200"
    router.dispatch({"user_id": 200, "op": "submit", "request_id": "one", "text": "task"})
    configure(100, [])
    with pytest.raises(ValueError):
        router.dispatch({"user_id": 200, "op": "root_check", "code": "ABCD1234"})
    assert created[200].cancel.is_set()
    assert created[200].db.next() is None
    created[200].root.cancel_all.assert_called_once()
    created[200].root.check.assert_not_called()
    assert not created[100].cancel.is_set()
    configure(200, [100])
    router.refresh()
    assert router.workers[200] is created[200]
    assert router.workers[100] is created[100]
    path.write_text("broken")
    with pytest.raises(ValueError):
        router.dispatch({"user_id": 100, "op": "health"})
    for worker in created.values():
        worker.db.db.close()


def test_legacy_dialog_cannot_follow_changes_to_owner_id(db):
    from codex_telegram_agent.worker import legacy_identity

    db.submit("telegram:100:1234", "original task")
    db.set("thread", "original-thread")
    assert legacy_identity(db, 200) == 100
    assert legacy_identity(db, 300) == 100
    assert db.get("thread") == "original-thread"
