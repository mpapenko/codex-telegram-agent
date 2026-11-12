import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from codex_telegram_agent.codex import AppServer
from codex_telegram_agent.root import RootApprovals
from codex_telegram_agent.worker import Worker


def test_only_current_waiting_code_can_be_approved():
    root = RootApprovals(Mock())
    root.request = Mock(return_value={"id": "request", "status": "reserved"})
    assert root.check("ABCD1234") == {"error": "invalid_code"}
    root.request.assert_not_called()
    root.waiting["ABCD1234"] = "request"
    assert root.check("ABCD1234") == {"ok": True, "id": "request"}
    root.request.assert_not_called()
    root.cancel_all()
    assert root.check("ABCD1234") == {"error": "invalid_code"}


def test_dynamic_tool_cannot_approve_itself_or_use_other_turn():
    async def run():
        app = AppServer(Path("/fake/state"), Path("/fake/projects"), root=AsyncMock())
        app.process = Mock(returncode=None)
        app.send = AsyncMock()
        app.thread_id = "thread"
        app.turn_id = "turn"
        for tool, turn in [("approve_root", "turn"), ("request_root_command", "old-turn")]:
            await app.tool_call(
                {"id": 1, "params": {"threadId": "thread", "turnId": turn, "tool": tool}}
            )
            app.root.execute.assert_not_called()
            text = app.send.call_args.args[0]["result"]["contentItems"][0]["text"]
            assert json.loads(text)["status"] == "rejected"

    asyncio.run(run())


def test_approval_code_only_goes_to_owner_outbox_not_model():
    db = Mock()
    root = RootApprovals(db)
    state = {"status": "pending"}
    proposed = {
        "id": "request",
        "code": "ABCD1234",
        "status": "pending",
        "reason": "Причина",
        "risk": "Ризик",
        "cwd": "/",
        "timeout": 600,
        "command": "id -u",
        "digest": "a" * 64,
    }

    def request(payload):
        if payload["op"] == "propose":
            return proposed
        if payload["op"] == "approve":
            state["status"] = "completed"
            return {"id": "request", "status": "reserved"}
        return {"status": state["status"], "result": {"exit_code": 0, "stdout": "fake"}}

    root.request = request
    root.a_request = AsyncMock(side_effect=request)

    async def run():
        task = asyncio.create_task(
            root.execute(
                {
                    "threadId": "thread",
                    "turnId": "turn",
                    "callId": "call",
                    "arguments": {
                        "command": "id -u",
                        "cwd": "/",
                        "reason": "Причина",
                        "risk": "Ризик",
                    },
                }
            )
        )
        await asyncio.sleep(0)
        assert not task.done()
        assert "дозволяю ABCD1234" in db.emit.call_args.args[0]
        assert root.check("ABCD1234") == {"ok": True, "id": "request"}
        state["status"] = "completed"  # Simulate a separate authenticated frontend approval.
        result = await task
        assert result["status"] == "completed"
        assert "ABCD1234" not in json.dumps(result)
        assert not root.waiting

    asyncio.run(run())


def test_broker_unavailable_does_not_request_password():
    root = RootApprovals(Mock())
    root.a_request = AsyncMock(side_effect=OSError("offline"))

    async def run():
        result = await root.execute(
            {"arguments": {"command": "id -u", "cwd": "/", "reason": "Причина", "risk": "Ризик"}}
        )
        assert result["status"] == "unavailable"
        assert "never request a password" in result["message"]
        root.db.emit.assert_not_called()

    asyncio.run(run())


def test_worker_socket_has_no_approval_operation():
    root = Mock()
    worker = Worker(Mock(), Mock(), root=root)
    with pytest.raises(ValueError, match="invalid_request"):
        worker.dispatch({"op": "root_approve", "code": "ABCD1234"})
    root.assert_not_called()
