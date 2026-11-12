import asyncio
import json
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from codex_telegram_agent.codex import AppServer
from codex_telegram_agent.host import HostTools


def invoke(host, action, scope="user", unit="luxpower-telegram-bot.service"):
    return asyncio.run(
        host.execute(
            {
                "arguments": {
                    "action": action,
                    "scope": scope,
                    "unit": unit,
                }
            }
        )
    )


def test_diagnostics_never_expose_command_arguments_or_environment():
    host = HostTools()
    host.run = AsyncMock(return_value=(0, "header\nprocess"))
    assert invoke(host, "overview", unit="")["status"] == "completed"
    calls = [call.args[0] for call in host.run.call_args_list]
    ps = next(command for command in calls if command[0] == "/usr/bin/ps")
    assert "pid,ppid,user,stat,pcpu,pmem,rss,comm" in ps
    assert not any("args" in arg or "environ" in arg for arg in ps)


def test_user_restart_needs_no_root_and_uses_fixed_arguments():
    host = HostTools()
    host.run = AsyncMock(return_value=(0, ""))
    assert invoke(host, "restart")["status"] == "completed"
    host.run.assert_awaited_once_with(
        [
            "/usr/bin/systemctl",
            "--user",
            "restart",
            "luxpower-telegram-bot.service",
        ]
    )


@pytest.mark.parametrize(
    "unit", ["--all", "../x.service", "x.service; id", "/tmp/x.service", "x\n.service"]
)
def test_service_name_cannot_inject_options_or_shell(unit):
    host = HostTools()
    host.run = AsyncMock()
    assert invoke(host, "restart", unit=unit)["status"] == "rejected"
    host.run.assert_not_called()


@pytest.mark.parametrize(
    "action,scope,unit",
    [
        ("restart", "system", "codex-root-broker.service"),
        ("stop", "user", "codex-telegram-agent.service"),
        ("logs", "user", "other.service"),
        ("set-property", "user", "luxpower-telegram-bot.service"),
    ],
)
def test_system_changes_and_arbitrary_control_channels_require_approval(action, scope, unit):
    host = HostTools()
    host.run = AsyncMock()
    assert invoke(host, action, scope, unit)["status"] == "approval_required"
    host.run.assert_not_called()


def test_status_cannot_read_environment_or_execstart():
    host = HostTools()
    host.run = AsyncMock(return_value=(0, "ActiveState=active"))
    assert invoke(host, "status", "system", "luxpower-automation.service")["status"] == "completed"
    command = host.run.call_args.args[0]
    assert "Environment" not in command
    assert "ExecStart" not in command
    assert "--user" not in command


def test_log_tokens_are_redacted():
    host = HostTools()
    host.run = AsyncMock(return_value=(0, "123456:" + "x" * 30))
    assert invoke(host, "logs")["result"]["text"] == "[TOKEN REDACTED]"


@pytest.mark.parametrize("arguments", [None, {}, {"action": [], "scope": [], "unit": ""}])
def test_invalid_tool_arguments_fail_closed(arguments):
    host = HostTools()
    host.run = AsyncMock()
    assert asyncio.run(host.execute({"arguments": arguments}))["status"] == "rejected"
    host.run.assert_not_called()


def test_host_tool_only_runs_in_current_thread_and_turn():
    async def run():
        host = Mock(execute=AsyncMock(return_value={"status": "completed"}))
        app = AppServer(Path("/state"), Path("/projects"), host=host)
        app.process = Mock(returncode=None)
        app.send = AsyncMock()
        app.thread_id, app.turn_id = "thread", "turn"
        for thread, turn in (("other", "turn"), ("thread", "old")):
            await app.tool_call(
                {
                    "id": 1,
                    "params": {
                        "threadId": thread,
                        "turnId": turn,
                        "tool": "host_system",
                        "arguments": {},
                    },
                }
            )
            host.execute.assert_not_called()
        await app.tool_call(
            {
                "id": 1,
                "params": {
                    "threadId": "thread",
                    "turnId": "turn",
                    "tool": "host_system",
                    "arguments": {},
                },
            }
        )
        host.execute.assert_awaited_once()
        result = app.send.call_args.args[0]["result"]
        assert json.loads(result["contentItems"][0]["text"])["status"] == "completed"

    asyncio.run(run())


def test_open_network_keeps_secret_and_control_channel_protections():
    config = tomllib.loads((Path(__file__).parents[1] / "deploy/codex-config.toml").read_text())
    assert config["features"]["network_proxy"] is False
    profile = config["permissions"]["telegram"]
    assert profile["network"]["enabled"] is True
    assert profile["network"]["allow_local_binding"] is True
    assert not profile["network"].get("domains")
    files = profile["filesystem"]
    assert files[":minimal"] == "read"
    assert "/" not in files
    assert files["/home/maksym/projects"] == "write"
    assert files["/run"] == "read"
    for name in (
        "luxpower-automation",
        "luxpower-telegram-bot",
        "codex-telegram-agent",
        "codex-root-broker",
    ):
        assert files["/home/maksym/projects/" + name] == "read"
    for path in ("/home/maksym/.ssh", "/home/maksym/.aws", "/run/user", "/run/codex-root-broker"):
        assert files[path] == "deny"
    assert files[":workspace_roots"]["**/.env"] == "deny"
    assert config["agents"]["enabled"] is False


def test_host_tool_registered_on_new_threads_and_explained_for_old_ones(tmp_path):
    from codex_telegram_agent.db import Database

    class FakeApp(AppServer):
        def __init__(self):
            super().__init__(Path("/state"), Path("/projects"), root=Mock(), host=Mock())
            self.calls = []

        async def start(self):
            pass

        async def request(self, method, params):
            self.calls.append((method, params))
            if method in {"thread/start", "thread/resume"}:
                return {"thread": {"id": "thread"}}
            await self.events.put(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread",
                        "turn": {"id": "turn", "status": "completed"},
                    },
                }
            )
            return {"turn": {"id": "turn"}}

    async def run():
        db = Database(tmp_path / "db.sqlite3")
        app = FakeApp()
        await app.run("task", db, asyncio.Event())
        names = {tool["name"] for tool in app.calls[0][1]["dynamicTools"]}
        assert names == {"host_system", "device_control", "request_root_command"}
        assert db.get("host_tools_thread") == "thread"
        db.set("host_tools_thread", "")
        await app.run("next", db, asyncio.Event())
        instructions = app.calls[2][1]["developerInstructions"]
        assert "host_system ще не зареєстровано" in instructions
        assert "/new" in instructions
        db.db.close()

    asyncio.run(run())


def test_device_control_uses_configured_cli_without_exposing_secrets_or_setting_hold():
    host = HostTools()
    host.run = AsyncMock(
        return_value=(
            0,
            json.dumps(
                {
                    "outcome": "confirmed",
                    "on": False,
                    "local_key": "DO_NOT_EXPOSE",
                }
            ),
        )
    )
    result = asyncio.run(
        host.execute(
            {
                "tool": "device_control",
                "arguments": {
                    "action": "off",
                    "device": "kitchen",
                },
            }
        )
    )
    assert result == {"status": "completed", "result": {"device": "kitchen", "on": False}}
    assert "DO_NOT_EXPOSE" not in json.dumps(result)
    host.run.assert_awaited_once_with(
        [
            "/home/maksym/projects/luxpower-automation/.venv/bin/luxpower-automation",
            "tuya-local",
            "off",
            "--device",
            "kitchen",
            "--execute",
        ]
    )


def test_uncertain_device_action_is_not_retried():
    host = HostTools()
    host.run = AsyncMock(return_value=(1, "private output"))
    result = asyncio.run(host.device({"action": "on", "device": "bathroom"}))
    assert result["status"] == "unknown"
    assert "private output" not in json.dumps(result)
    host.run.assert_awaited_once()


@pytest.mark.parametrize(
    "args",
    [
        {"action": "hold", "device": "kitchen"},
        {"action": "on", "device": "kitchen; id"},
        {"action": [], "device": "test"},
    ],
)
def test_device_control_does_not_accept_arbitrary_commands(args):
    host = HostTools()
    host.run = AsyncMock()
    assert asyncio.run(host.device(args))["status"] == "rejected"
    host.run.assert_not_called()
