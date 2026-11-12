"""Privileged tool bridge. Codes go to the owner outbox, never to the model."""

import asyncio
import contextlib
import json
import socket

ROOT_TOOL = {
    "type": "function",
    "name": "request_root_command",
    "description": (
        "Propose one exact root shell command for the owner's explicit, single-use "
        "Telegram approval. This waits for approval and execution; never approve it yourself. "
        "Use only when ordinary sandboxed tools cannot perform the requested system action. "
        "Never request a password. Never automatically repeat an uncertain command."
    ),
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "command": {
                "type": "string",
                "description": "Exact bash command, no sudo prefix needed.",
            },
            "cwd": {
                "type": "string",
                "description": "Absolute working directory on the real host.",
            },
            "reason": {
                "type": "string",
                "description": "Explain in Ukrainian why this exact command is needed.",
            },
            "risk": {
                "type": "string",
                "description": "Explain effects/risks in Ukrainian, including destructive effects.",
            },
        },
        "required": ["command", "cwd", "reason", "risk"],
    },
}


class RootApprovals:
    def __init__(self, db, path="/run/codex-root-broker/broker.sock"):
        self.db, self.path = db, str(path)
        self.waiting = {}
        self.cancelled = set()

    def request(self, payload):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(self.path)
            client.sendall(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
            with client.makefile("rb") as reader:
                raw = reader.readline(256_001)
            return self.parse(raw)

    @staticmethod
    def parse(raw):
        if len(raw) > 256_000:
            raise ValueError("response_too_large")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("broker_rejected_request")
        if "error" in result:
            code = result["error"]
            raise ValueError(
                code
                if code in {"busy", "invalid_code", "rate_limit"}
                else "broker_rejected_request"
            )
        return result

    async def a_request(self, payload):
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self.path, limit=256_001), 2
        )
        try:
            writer.write(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
            await asyncio.wait_for(writer.drain(), 2)
            return self.parse(await asyncio.wait_for(reader.readline(), 2))
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    def check(self, code):
        if not isinstance(code, str):
            return {"error": "invalid_code"}
        row_id = self.waiting.get(code)
        if row_id is None or row_id in self.cancelled:
            return {"error": "invalid_code"}
        return {"ok": True, "id": row_id}

    def cancel_all(self):
        self.cancelled.update(self.waiting.values())

    async def execute(self, params):
        args = params.get("arguments")
        if not isinstance(args, dict) or set(args) != {"command", "cwd", "reason", "risk"}:
            return {"status": "rejected", "message": "Invalid root tool arguments."}
        if any(not isinstance(args[key], str) for key in args):
            return {"status": "rejected", "message": "Invalid root tool arguments."}
        key = f"{params.get('threadId')}:{params.get('turnId')}:{params.get('callId')}"
        try:
            proposed = await self.a_request({"op": "propose", "request_key": key, **args})
        except (OSError, ValueError):
            return {
                "status": "unavailable",
                "message": (
                    "Root broker unavailable or request rejected. Ask owner to install/check "
                    "it via SSH; never request a password in Telegram."
                ),
            }
        code, row_id = proposed["code"], proposed["id"]
        self.waiting[code] = row_id
        try:
            if proposed["status"] == "pending":
                self.db.emit(
                    "🔐 Запит на виконання від root\n\n"
                    f"Причина: {proposed['reason']}\n\nРизики: {proposed['risk']}\n\n"
                    f"Робоча папка: {proposed['cwd']}\nЛіміт: {proposed['timeout']} с\n\n"
                    f"Точна команда:\n{proposed['command']}\n\n"
                    f"Відбиток: {proposed['digest'][:12]}\n\n"
                    "Результат команди повернеться агенту. Дозвіл діє 5 хвилин і лише один раз.\n"
                    f"Для виконання: дозволяю {code}\nДля відмови: скасовую {code}\n\n"
                    "Пароль не потрібен."
                )
            while True:
                if row_id in self.cancelled:
                    current = await self.a_request({"op": "cancel", "id": row_id})
                else:
                    current = await self.a_request({"op": "status", "id": row_id})
                if current["status"] in {"completed", "failed", "unknown", "expired", "cancelled"}:
                    return {
                        "status": current["status"],
                        "result": current.get("result"),
                        "message": (
                            "Never automatically repeat this command. "
                            "If uncertain, ask owner to inspect state."
                        ),
                    }
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            try:
                current = await self.a_request({"op": "cancel", "id": row_id})
                if current.get("status") == "running":
                    self.db.emit(
                        "⚠️ Root-команда вже виконується окремим сервісом. "
                        "Зупинка Codex її не скасовує; перевірте результат перед повторною дією."
                    )
            except (OSError, ValueError):
                self.db.emit(
                    "⚠️ Стан root-команди невідомий після розриву зв’язку. "
                    "Не повторюйте її без перевірки."
                )
            raise
        except (OSError, ValueError):
            return {
                "status": "unknown",
                "message": (
                    "Broker connection lost. Execution may have started. "
                    "Do not retry automatically."
                ),
            }
        finally:
            self.waiting.pop(code, None)
            self.cancelled.discard(row_id)
