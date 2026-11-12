# Private Codex worker for Telegram

## Optional root broker

The owner-authorized `../codex-root-broker` integration adds a dynamic tool to new
conversations. Ordinary tools use the permission policy described below. The tool proposes
an exact root command or complete approved host-change batch and waits for a separate owner-and-extended-user Telegram approval;
this worker cannot approve commands itself. No passwords are used. See that
project's README for the one-time SSH installation, real root trust boundary, and
first manual test. Existing dialogues need `/new` to register the new tool; their
history is preserved rather than silently reset. `/stop` cancels pending requests,
but an approved root command already running may continue independently.
Ordinary wording such as "Запусти id -u через sudo" is routed to the root tool;
the owner does not need to name `request_root_command`. If an older conversation
has no registered tool, the agent explains `/new` rather than refusing root use.

Separate local process for the owner-and-extended-user Codex mode in `luxpower-telegram-bot`.
Python 3.13+ standard library runtime; uses the installed official Codex app-server
over stdio. No HTTP listener, Telegram token, or sudo password is stored here.

## Conversation

The owner or an extended user opens **🤖 Чат із Codex** in the existing bot. The reply keyboard is
removed and subsequent text becomes agent input rather than boiler commands.
Only authenticated owner/extended private chats can submit prompts or receive
responses. Each identity has its own SQLite queue, thread pointer, responses and
root requests. `/stop`, `/new` and root approval operate only on that identity.
The owner retains the original `worker.sqlite3` and dialog; extended users use
`worker-<id>.sqlite3`. Projects on disk remain shared. A global execution lock
serializes jobs across users to avoid concurrent edits. Entering chat mode cancels pending boiler confirmations; old boiler
confirmation callbacks are rejected while that user is in agent mode.

- `/boilers` or `до бойлерів`: return to the boiler menu. Running work continues.
- `/stop` or `зупини завдання`: cancel queued work and request cancellation of the
  active task. Already completed changes are not rolled back.
- `/new` or `новий діалог`: start a fresh thread when no work is active or queued.

No inner chat buttons or project/model selectors are required. Name the project
and desired result in ordinary text. Luna high is fixed for this initial release.
This is a separate conversation from the setup chat and has no automatic access
to its history. Up to five messages/tasks are queued, processed serially against
the saved Codex thread. Messages do not steer an in-progress turn; they become
subsequent turns. Each turn is limited to 30 minutes. Queued tasks older than
15 minutes expire rather than performing stale instructions.

## Security and deployment boundary

The agent runs as the normal Unix user through `setpriv --no-new-privs` and a
dedicated Codex filesystem/network profile. No approval/escalation requests are
granted. Requests for missing information should be plain Ukrainian messages.

New projects remain writable under `/home/maksym/projects`; all four live
project source trees remain read-only. Secret filename patterns, SSH/AWS/login
credentials, private configs and approval queues stay denied. Saved controller
JSON and plug observations are readable. The raw Telegram SQLite outbox and
worker runtime are still private because they contain conversations/root codes.

Outside-project writes and deployment of protected code use a **single approved
batch** through `request_root_command`: list all files/services/actions in the
reason, show their effects, and include inline content or verify source hashes.
One approval covers the whole fixed change set, not one file. Ordinary user-file
operations in the approved batch should run as `maksym` (`runuser -u maksym --`);
root is used only where needed. Native sandbox escalation remains disabled.
Direct outside-project writes stay technically blocked so this approval cannot
be bypassed. New unapproved actions require a new batch rather than broadening
an already approved request.

Prepared source copies of all four projects live under
`/home/maksym/projects/codex-workspaces/`, excluding secrets/state/Git/venvs.
Existing copies are never overwritten. Agent work can edit/test these copies
and propose a reviewed deployment batch. Copies are snapshots, not automatic
mirrors: inspect existing unfinished work before updating them.

Internet and LAN command access are now unrestricted by domain (`network_proxy`
is disabled); local binding is enabled. Native live web search remains enabled.
The general instruction bans on equipment, messages, services and scheduling
have been removed. Public Git pushes and subagents remain disallowed.

The `host_system` dynamic tool supplies actual host CPU/RAM/disk/process data,
selected service fields, logs of the four controlled services, and user-service
start/stop/restart without an approval code. It executes fixed argv as `maksym`,
not arbitrary shell commands. Process arguments/environments, arbitrary log
sources and manager property changes are not exposed. System-service mutations
and restarting the worker itself use an approved batch. The latter would kill
the active tool turn if performed inline.

The `device_control` dynamic tool reads/switches `bathroom`, `kitchen` or `test`
through the existing controller CLI on the host. Credentials stay private in
the CLI. ON/OFF is immediate, does not set/release a hold, and never retries an
uncertain result. Development tests use fake outputs; no live switches are run.

Local runtime sockets under `/run` are readable. Direct `/proc` and private
D-Bus/approval sockets stay hidden to prevent secret
reads and self-approval; `host_system` provides the requested functionality.
An unrestricted user-manager bus could rewrite the trusted frontend environment
and bypass its role boundary, so only explicit service operations are bridged.
New conversations register `host_system` and `device_control`; existing chats
need `/new` once, after work completes. Their old history is not deleted.

The worker has its own Codex config/history under
`~/.local/share/codex-telegram/codex` and initially references the existing CLI
authentication file through a private symlink. Only the harness reads it; agent
tools cannot. Do not pass secrets in Telegram. The frontend rejects obvious key
and password patterns, but that heuristic is not comprehensive secret detection.
Never treat chat confirmation as an OS authentication grant.

Filesystem profiles are beta. Real installed sandbox probes verified project
writes, denied credential/approval-database opens (without reading contents),
denied direct outside/live-code writes, access to a harmless localhost server
and an HTTPS host outside the former allowlist, and denial of direct manager-bus
access. Host process diagnostics are separately bridged. These controls do not protect
against another unrestricted process using the same Unix account.

## Queues and failures

Prompt/job IDs are unique and durable. Jobs are reserved before execution.
After a restart, started jobs become interrupted and are never replayed. Work
can have partially completed before interruption, so inspect it before retrying.
Queued jobs may proceed after recovery while still fresh. Thread IDs persist,
including across bot restarts. New dialogue resets only the saved thread pointer.

Only agent messages are relayed, not command stdout, reasoning traces, or raw
protocol errors. Progress heartbeats appear after a minute. Responses are split
into Telegram-sized text messages. A private durable worker outbox is transferred
to the bot's durable outbox before acknowledgement. Stable non-reused IDs prevent
duplicate transfer after a connection failure. Telegram delivery can still be
duplicated after an ambiguous send failure. Never delete runtime state to retry.

The local Unix socket verifies peer UID and is mode 0600. Code running as the
same unrestricted host account is trusted. The sandbox cannot access that socket.
The bot continues polling events/boiler state while the worker runs.

## User service

The bot exports a private identities-only `state/codex-access.json` on startup.
Start the bot before the first worker startup. After membership changes, restart
only the bot (`restart-bot`): the worker refreshes the exported allowlist before
IPC requests and between jobs. New users get an isolated queue without a worker
restart. Removed users lose IPC access immediately on refresh; queued work and
pending root requests are cancelled, active work is asked to stop. The legacy
SQLite dialog is permanently bound to its original sender, inferred once from
its oldest Telegram request, so changing owner_id cannot reassign that dialog.
The worker never reads the Telegram token or private bot config.
Unknown/missing IPC user IDs are rejected.


```sh
cd /home/maksym/projects/codex-telegram-agent
python3 -m venv .venv
python3 deploy/provision.py
mkdir -p ~/.config/systemd/user
install -m 644 deploy/codex-telegram-agent.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now codex-telegram-agent.service
```

The installed venv may omit pip: runtime needs no third-party packages.
`loginctl enable-linger maksym` enables the user manager at boot and keeps it
running after logout; OS policy may require administrator approval for that
one-time account setting. It does not give the agent sudo access.

Changing the policy requires reprovisioning and restarting the worker. Do not
overwrite edited source copies during reprovisioning; the provisioning script
deliberately leaves existing copies alone. The bot socket path is
`/home/maksym/projects/luxpower-telegram-bot/state/codex.sock`.

## Verification

Tests use synthetic databases and fake app-server transports:

```sh
python -m pytest
python -m ruff check .
python -m ruff format --check .
```

A separate live smoke check used a disposable directory to verify a real Luna
turn could create a permitted test file and return a message. It did not query or
switch equipment, modify real projects, or invoke sudo.

Official references: [app-server](https://learn.chatgpt.com/docs/app-server),
[permission profiles](https://learn.chatgpt.com/docs/permissions).


Інструкція перенесення сервера: [Розгортання та відновлення](DEPLOYMENT.md).
