"""User-owned Unix socket service with a single serial Codex task queue."""

import asyncio
import contextlib
import fcntl
import json
import os
import socket
import struct
from pathlib import Path

from .codex import AppServer
from .db import Database
from .root import RootApprovals


class Worker:
    def __init__(self, db, agent, root=None, execution_lock=None):
        self.db, self.agent = db, agent
        self.cancel = asyncio.Event()
        self.active = False
        self.root = root
        self.execution_lock = execution_lock or asyncio.Lock()

    async def heartbeat(self):
        while True:
            await asyncio.sleep(60)
            self.db.emit("⏳ Codex ще працює. Для зупинки надішліть /stop.")

    def dispatch(self, data):
        if not isinstance(data, dict):
            raise ValueError("invalid_request")
        operation = data.get("op")
        if operation == "submit":
            text, request = data.get("text"), data.get("request_id")
            if not isinstance(text, str) or not text.strip() or len(text) > 12_000:
                raise ValueError("invalid_request")
            if not isinstance(request, str) or len(request) > 100:
                raise ValueError("invalid_request")
            return {"job": self.db.submit(request, text)}
        if operation == "poll":
            return {"messages": self.db.poll()}
        if operation == "ack":
            ids = data.get("ids")
            if not isinstance(ids, list) or len(ids) > 10 or any(type(i) is not int for i in ids):
                raise ValueError("invalid_request")
            self.db.ack(ids)
            return {"ok": True}
        if operation == "stop":
            self.db.cancel_queued()
            self.cancel.set()
            if self.root is not None:
                self.root.cancel_all()
            return {"ok": True, "root_pending": bool(self.root and self.root.waiting)}
        if operation == "root_check":
            if self.root is None:
                return {"error": "unavailable"}
            return self.root.check(data.get("code"))
        if operation == "new":
            if (
                self.active
                or self.db.db.execute("SELECT 1 FROM jobs WHERE status='queued'").fetchone()
            ):
                raise ValueError("busy")
            self.db.set("thread", "")
            return {"ok": True}
        if operation == "health":
            return {"ok": True, "active": self.active}
        raise ValueError("invalid_request")

    async def handle(self, reader, writer):
        try:
            peer = writer.get_extra_info("socket").getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            if struct.unpack("3i", peer)[1] != os.getuid():
                return
            line = await asyncio.wait_for(reader.readline(), 3)
            if len(line) > 64_000:
                return
            result = self.dispatch(json.loads(line))
        except ValueError as error:
            code = str(error)
            result = {"error": code if code in {"busy", "queue_full"} else "invalid_request"}
        except Exception:
            result = {"error": "unavailable"}
        finally:
            if "result" in locals():
                writer.write(json.dumps(result, ensure_ascii=False).encode() + b"\n")
                with contextlib.suppress(OSError):
                    await writer.drain()
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def work(self):
        self.db.recover()
        while True:
            async with self.execution_lock:
                worked = await self.work_one()
            if not worked:
                await asyncio.sleep(0.5)

    async def work_one(self):
        job = self.db.next()
        if job is None:
            return False
        job_id, prompt = job
        self.active = True
        self.cancel.clear()
        self.db.emit("🤖 Працюю над вашим повідомленням…")
        heartbeat = asyncio.create_task(self.heartbeat())
        try:
            status = await asyncio.wait_for(self.agent.run(prompt, self.db, self.cancel), 1800)
            if status != "completed":
                self.db.emit(
                    "⚠️ Завдання зупинено або завершилося помилкою. "
                    "Автоматичного повтору не буде; перевірте зроблені зміни."
                )
                await self.agent.close()
            self.db.finish(job_id, status)
        except Exception:
            self.db.finish(job_id, "failed")
            self.db.emit(
                "⚠️ Не вдалося завершити завдання Codex. Перевірте вхід CLI, "
                "підключення або обмеження доступу. Завдання не повторюється автоматично."
            )
            await self.agent.close()
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            self.active = False
        return True


class Router:
    """Each configured identity has its own queue, dialog, output and root codes."""

    def __init__(self, workers, access_path=None, factory=None):
        self.workers = workers
        self.allowed = set(workers)
        self.access_path, self.factory = access_path, factory

    def refresh(self):
        if self.access_path is None:
            return
        access = json.loads(self.access_path.read_text())
        owner, extended = access["owner_id"], access["extended_user_ids"]
        if type(owner) is not int or owner <= 0 or not isinstance(extended, list):
            raise ValueError("invalid_access")
        if any(type(user) is not int or user <= 0 for user in extended):
            raise ValueError("invalid_access")
        allowed = set(extended) | {owner}
        for user_id in sorted(allowed - self.workers.keys()):
            self.workers[user_id] = self.factory(user_id)
        for user_id in self.allowed - allowed:
            self.workers[user_id].dispatch({"op": "stop"})
        self.allowed = allowed

    def dispatch(self, data):
        if not isinstance(data, dict) or type(data.get("user_id")) is not int:
            raise ValueError("invalid_request")
        self.refresh()
        if data["user_id"] not in self.allowed:
            raise ValueError("invalid_request")
        return self.workers[data["user_id"]].dispatch(data)

    async def work(self):
        while True:
            try:
                self.refresh()
            except (OSError, ValueError, KeyError, TypeError):
                await asyncio.sleep(0.5)
                continue
            # One serial queue runner across identities. IPC stays responsive and
            # can revoke an active identity while its agent is awaiting a result.
            for user_id, worker in list(self.workers.items()):
                if user_id in self.allowed:
                    await worker.work_one()
                elif worker.agent.process is not None:
                    await worker.agent.close()
            await asyncio.sleep(0.5)

    handle = Worker.handle


def legacy_identity(legacy, fallback_owner):
    legacy_owner = legacy.get("identity_owner")
    if legacy_owner is None:
        # Bind the legacy database once to its original sender, rather than
        # reassigning that person's conversation when owner_id is edited.
        row = legacy.db.execute(
            "SELECT request_id FROM jobs WHERE request_id LIKE 'telegram:%' "
            "ORDER BY created LIMIT 1"
        ).fetchone()
        legacy_owner = row[0].split(":")[1] if row else str(fallback_owner)
        if not legacy_owner.isdecimal() or int(legacy_owner) <= 0:
            raise ValueError("invalid_legacy_identity")
        legacy.set("identity_owner", legacy_owner)
    return int(legacy_owner)


async def serve(state: Path, workspace: Path, socket_path: Path):
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (state / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        access_path = socket_path.parent / "codex-access.json"
        access = json.loads(access_path.read_text())
        legacy = Database(state / "worker.sqlite3")
        legacy_owner = legacy_identity(legacy, access["owner_id"])
        legacy.db.close()
        workers = {}

        def factory(user_id):
            path = state / (
                "worker.sqlite3" if user_id == int(legacy_owner) else f"worker-{user_id}.sqlite3"
            )
            db = Database(path)
            db.recover()
            root = RootApprovals(db)
            return Worker(db, AppServer(state, workspace, root), root)

        router = Router(workers, access_path, factory)
        router.refresh()
        socket_path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(router.handle, path=socket_path, limit=64_001)
        socket_path.chmod(0o600)
        try:
            async with server:
                await asyncio.gather(server.serve_forever(), router.work())
        finally:
            for worker in workers.values():
                await worker.agent.close()
                worker.db.db.close()
            socket_path.unlink(missing_ok=True)
