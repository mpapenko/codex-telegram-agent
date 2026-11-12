"""Host diagnostics and user-service operations without a general shell escape."""

import asyncio
import contextlib
import json
import os
import re
import signal
from pathlib import Path

HOST_TOOL = {
    "type": "function",
    "name": "host_system",
    "description": (
        "Inspect real host CPU/memory/processes, service status/logs, or manage user "
        "services without root approval. Never returns process arguments, environments, "
        "credentials or approval queues. System-service changes and arbitrary host file "
        "changes require request_root_command: combine the full change set in one approval."
    ),
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "overview",
                    "status",
                    "logs",
                    "start",
                    "stop",
                    "restart",
                ],
            },
            "scope": {"type": "string", "enum": ["user", "system"]},
            "unit": {"type": "string", "description": "Unit name; empty for overview."},
        },
        "required": ["action", "scope", "unit"],
    },
}

DEVICE_TOOL = {
    "type": "function",
    "name": "device_control",
    "description": (
        "Read or switch an existing configured Tuya plug on the real host without "
        "exposing its credentials. ON/OFF execute immediately without setting a hold. "
        "Never repeat an uncertain switch automatically."
    ),
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["status", "on", "off"]},
            "device": {"type": "string", "enum": ["bathroom", "kitchen", "test"]},
        },
        "required": ["action", "device"],
    },
}

STATUS_FIELDS = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "NRestarts",
    "ExecMainStatus",
    "MemoryCurrent",
    "CPUUsageNSec",
    "ActiveEnterTimestamp",
)
LOG_UNITS = {
    "user": {"luxpower-telegram-bot.service", "codex-telegram-agent.service"},
    "system": {"luxpower-automation.service", "codex-root-broker.service"},
}


class HostTools:
    async def run(self, args):
        env = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HOME": str(Path.home()),
            "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus",
            "SYSTEMD_PAGER": "cat",
            "SYSTEMD_COLORS": "0",
        }
        process = await asyncio.create_subprocess_exec(
            *args,
            env=env,
            cwd=(
                "/home/maksym/projects/luxpower-automation"
                if args[0]
                == "/home/maksym/projects/luxpower-automation/.venv/bin/luxpower-automation"
                else "/"
            ),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:

            async def read_output():
                kept = bytearray()
                while chunk := await process.stdout.read(4096):
                    kept.extend(chunk[: max(0, 24_000 - len(kept))])
                await process.wait()
                return bytes(kept)

            output = await asyncio.wait_for(read_output(), 15)
            return process.returncode, output.decode(errors="replace")
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()

    async def execute(self, params):
        if params.get("tool") == "device_control":
            return await self.device(params.get("arguments"))
        args = params.get("arguments")
        if not isinstance(args, dict) or set(args) != {"action", "scope", "unit"}:
            return {"status": "rejected", "message": "Invalid host tool arguments."}
        action, scope, unit = (args[key] for key in ("action", "scope", "unit"))
        if (
            not all(isinstance(value, str) for value in (action, scope, unit))
            or scope not in {"user", "system"}
            or len(unit) > 200
        ):
            return {"status": "rejected", "message": "Invalid host tool arguments."}
        if action == "overview" and unit == "":
            commands = {
                "uptime": ["/usr/bin/uptime"],
                "memory": ["/usr/bin/free", "-m"],
                "cpu_sample": ["/usr/bin/vmstat", "1", "2"],
                "disk": ["/usr/bin/df", "-h", "/"],
                "processes": [
                    "/usr/bin/ps",
                    "-eo",
                    "pid,ppid,user,stat,pcpu,pmem,rss,comm",
                    "--sort=-rss",
                ],
            }
            values = {}
            for key, command in commands.items():
                code, text = await self.run(command)
                values[key] = {"exit_code": code, "text": "\n".join(text.splitlines()[:25])}
            return {"status": "completed", "result": values}
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*\.(service|timer)", unit):
            return {"status": "rejected", "message": "Expected a service or timer name."}
        prefix = ["/usr/bin/systemctl"] + (["--user"] if scope == "user" else [])
        if action == "status":
            command = prefix + ["show", unit, "--no-pager"]
            for field in STATUS_FIELDS:
                command += ["-p", field]
        elif action == "logs" and unit in LOG_UNITS[scope]:
            command = ["/usr/bin/journalctl"] + (["--user"] if scope == "user" else [])
            command += ["-u", unit, "-n", "40", "--no-pager", "--output=short-iso"]
        elif action in {"start", "stop", "restart"} and scope == "user":
            if unit == "codex-telegram-agent.service":
                return {
                    "status": "approval_required",
                    "message": (
                        "Restarting/stopping this worker would interrupt the active turn. "
                        "Include it in an explicitly approved host command instead."
                    ),
                }
            command = prefix + [action, unit]
        else:
            return {
                "status": "approval_required",
                "message": (
                    "Use one request_root_command for the complete system change set. "
                    "Arbitrary service logs may contain secrets; use the documented app logs."
                ),
            }
        code, text = await self.run(command)
        # Application logs should already be sanitized; retain a second token filter.
        text = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b", "[TOKEN REDACTED]", text)
        text = re.sub(r"\bsk-[A-Za-z0-9_-]{15,}\b", "[TOKEN REDACTED]", text)
        return {
            "status": "completed" if code == 0 else "failed",
            "result": {
                "exit_code": code,
                "text": text,
            },
        }

    async def device(self, args):
        if not isinstance(args, dict) or set(args) != {"action", "device"}:
            return {"status": "rejected"}
        action, device = args["action"], args["device"]
        if not isinstance(action, str) or not isinstance(device, str):
            return {"status": "rejected"}
        if action not in {"status", "on", "off"} or device not in {"bathroom", "kitchen", "test"}:
            return {"status": "rejected"}
        command = [
            "/home/maksym/projects/luxpower-automation/.venv/bin/luxpower-automation",
            "tuya-local",
            action,
            "--device",
            device,
        ]
        if action != "status":
            command.append("--execute")
        try:
            code, text = await self.run(command)
            data = json.loads(text)
            on = data.get("on") if isinstance(data, dict) else None
            if type(on) is not bool:
                raise ValueError
            if action != "status" and (
                code != 0
                or on is not (action == "on")
                or data.get("outcome") not in {"confirmed", "already_in_requested_state"}
            ):
                raise ValueError
            if code != 0:
                raise ValueError
            return {"status": "completed", "result": {"device": device, "on": on}}
        except (OSError, ValueError, TimeoutError):
            return {
                "status": "unknown",
                "message": (
                    "Result unavailable or uncertain. Read plug status before any further action; "
                    "never automatically repeat the switch. No hold was changed."
                ),
            }
