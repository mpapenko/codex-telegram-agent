"""JSON-RPC client for the installed Codex app-server. No public listener."""

import asyncio
import contextlib
import json
import os
import signal
from pathlib import Path

from .root import ROOT_TOOL

INSTRUCTIONS = """Ти приватний агент власника, який отримує завдання через Telegram.
Відповідай українською, короткими абзацами. Працюй автономно в /home/maksym/projects:
досліджуй документацію, читай AGENTS.md потрібного проєкту перед змінами, створюй
і редагуй код, запускай доречні тести. Якщо назва проєкту неочевидна — запитай.
Чинні проєкти luxpower-automation та luxpower-telegram-bot доступні лише для читання.
Для змін у них працюй з підготовленими копіями в /home/maksym/projects/codex-workspaces/.
Нові проєкти створюй у /home/maksym/projects/. Зміни чинних сервісів спочатку
підготуй і протестуй у робочих копіях. Застосування можливе лише окремою точною
root-командою через request_root_command і разове погодження власника.
Не запускай sudo, su чи doas напряму в пісочниці та не обходь ізоляцію. Не запитуй паролі.
Не читай токени, .env, приватні конфігурації, SSH-ключі та операційний стан.
Не керуй реальними розетками, не змінюй системний розклад чи служби, не надсилай
повідомлення іншим людям і не публікуй код без конкретного запиту власника.
Для системних дій, які потребують root, є окремий інструмент request_root_command.
Коли власник каже «через sudo», «з правами root» або просить адміністративну дію,
це запит скористатися request_root_command, а не підстава відмовити. Приклад:
«Запусти id -u через sudo» означає запропонувати command="id -u", cwd="/"
через цей інструмент і дочекатися окремого підтвердження. Не замінюй запит
виконанням звичайного id -u без root. Префікс sudo у команді брокера не потрібен.
Запропонуй точну команду, робочу папку, причину й конкретні ризики. Виконання
відбудеться лише після разового підтвердження власника через Telegram. Не трактуй
звичайний текст користувача як уже виданий дозвіл. Не вигадуй кодів підтвердження.
Не повторюй автоматично root-команду з невідомим результатом. Якщо інструмента немає
в старому діалозі, попроси власника почати /new для підключення нової можливості.
Пряме sudo у пісочниці заборонене. Питання став звичайним текстом і завершуй хід.
Не створюй підлеглих агентів. Це окрема сесія, попереднього чату ChatGPT ти не бачиш.
Ніколи не стверджуй, що щось запущено, протестовано чи опубліковано без перевірки.
"""


class CodexError(Exception):
    pass


class AppServer:
    def __init__(self, state: Path, workspace: Path, root=None):
        self.state, self.workspace = state, workspace
        self.process = None
        self.reader = None
        self.pending = {}
        self.events = asyncio.Queue()
        self.counter = 0
        self.turn_id = None
        self.root = root
        self.tool_tasks = set()
        self.closing = False
        self.thread_id = None

    async def start(self):
        if self.process is not None and self.process.returncode is None:
            return
        if self.process is not None:
            await self.close()
        self.closing = False
        env = {
            key: os.environ[key]
            for key in ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "SSL_CERT_FILE")
            if key in os.environ
        }
        env.update(
            CODEX_HOME=str(self.state / "codex"),
            PATH="/home/maksym/.local/bin:/usr/local/bin:/usr/bin:/bin",
        )
        self.events = asyncio.Queue()
        self.process = await asyncio.create_subprocess_exec(
            "/usr/bin/setpriv",
            "--no-new-privs",
            "--",
            "/home/maksym/.local/bin/codex",
            "app-server",
            "--listen",
            "stdio://",
            cwd=self.workspace,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=2_000_000,
        )
        self.reader = asyncio.create_task(self.read())
        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "private_telegram",
                    "title": "Приватний Telegram",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.send({"method": "initialized", "params": {}})

    async def send(self, data):
        self.process.stdin.write(json.dumps(data, ensure_ascii=False).encode() + b"\n")
        await self.process.stdin.drain()

    async def request(self, method, params):
        self.counter += 1
        number = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[number] = future
        try:
            await self.send({"id": number, "method": method, "params": params})
            return await asyncio.wait_for(future, 90)
        finally:
            self.pending.pop(number, None)

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                data = json.loads(line)
                if "method" in data and "id" in data:
                    if (
                        data["method"] == "item/tool/call"
                        and self.root is not None
                        and not self.closing
                    ):
                        task = asyncio.create_task(self.tool_call(data))
                        self.tool_tasks.add(task)
                        task.add_done_callback(self.tool_tasks.discard)
                        continue
                    # No permission grant, shell escalation, or hidden interactive approval.
                    await self.send(
                        {
                            "id": data["id"],
                            "error": {
                                "code": -32000,
                                "message": (
                                    "Interactive tool/permission requests are disabled; "
                                    "ask the owner in plain text."
                                ),
                            },
                        }
                    )
                elif "id" in data:
                    future = self.pending.get(data["id"])
                    if future and not future.done():
                        if "error" in data:
                            future.set_exception(CodexError("Codex request failed."))
                        else:
                            future.set_result(data.get("result"))
                elif data.get("method") in {"item/completed", "turn/completed"}:
                    await self.events.put(data)
        except Exception:
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(CodexError("Codex disconnected."))
            await self.events.put({"method": "disconnected"})

    async def tool_call(self, data):
        params = data.get("params", {})
        if (
            params.get("tool") != "request_root_command"
            or params.get("threadId") != self.thread_id
            or params.get("turnId") != self.turn_id
            or self.turn_id is None
            or self.closing
        ):
            result = {"status": "rejected", "message": "Unknown tool or inactive task context."}
        else:
            try:
                result = await self.root.execute(params)
            except Exception:
                result = {
                    "status": "unknown",
                    "message": "Privileged request failed. Do not retry automatically.",
                }
        if self.process is None or self.process.returncode is not None:
            return
        with contextlib.suppress(OSError, ConnectionError):
            await self.send(
                {
                    "id": data["id"],
                    "result": {
                        "success": result.get("status") == "completed",
                        "contentItems": [
                            {"type": "inputText", "text": json.dumps(result, ensure_ascii=False)}
                        ],
                    },
                }
            )

    async def run(self, prompt, db, cancel: asyncio.Event):
        await self.start()
        thread = db.get("thread")
        options = {
            "cwd": str(self.workspace),
            "model": "gpt-6-luna",
            "permissions": "telegram",
            "approvalPolicy": "never",
            "developerInstructions": INSTRUCTIONS,
        }
        if self.root is not None and thread and db.get("root_tools_thread") != thread:
            options["developerInstructions"] += (
                "\nСтан саме цього діалогу: root-інструмент ще НЕ зареєстрований. "
                "Для запиту sudo/root поясни: сервіс разових дозволів підтримується, "
                "але треба надіслати /new у Telegram для нового діалогу з інструментом, "
                "а потім повторити завдання. Не стверджуй, що root-доступ узагалі заборонений. "
                "Не скидай діалог сам і не виконуй замість цього непривілейовану команду."
            )
        if thread:
            result = await self.request(
                "thread/resume", {**options, "threadId": thread, "excludeTurns": True}
            )
        else:
            if self.root is not None:
                options["dynamicTools"] = [ROOT_TOOL]
            result = await self.request("thread/start", options)
            if self.root is not None:
                db.set("root_tools_thread", result["thread"]["id"])
        thread = result["thread"]["id"]
        self.thread_id = thread
        db.set("thread", thread)
        self.turn_id = None
        result = await self.request(
            "turn/start",
            {
                "threadId": thread,
                "input": [{"type": "text", "text": prompt}],
                "effort": "high",
                "permissions": "telegram",
                "approvalPolicy": "never",
            },
        )
        self.turn_id = result["turn"]["id"]
        count = 0
        while True:
            if cancel.is_set():
                await self.request("turn/interrupt", {"threadId": thread, "turnId": self.turn_id})
                await self.close()
                return "interrupted"
            try:
                event = await asyncio.wait_for(self.events.get(), 1)
            except TimeoutError:
                continue
            if event["method"] == "disconnected":
                raise CodexError("Codex disconnected.")
            params = event.get("params", {})
            if params.get("threadId") != thread:
                continue
            if event["method"] == "turn/completed":
                if params.get("turn", {}).get("id") == self.turn_id:
                    status = params["turn"]["status"]
                    if count == 0 and status == "completed":
                        db.emit("✅ Codex завершив завдання без текстової відповіді.")
                    return status
            elif params.get("turnId") == self.turn_id:
                item = params.get("item", {})
                if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                    if (count < 25 or item.get("phase") == "final_answer") and item["text"]:
                        db.emit(item["text"])
                        count += 1

    async def close(self):
        self.closing = True
        tasks = list(self.tool_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.process and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        self.process = None
