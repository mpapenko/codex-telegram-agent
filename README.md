# Private Codex worker for Telegram

## Optional root broker

The owner-authorized `../codex-root-broker` integration adds a dynamic tool to new
conversations. Ordinary tools retain all existing restrictions. The tool proposes
an exact privileged command and waits for a separate owner-and-extended-user Telegram approval;
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

Filesystem access is limited to runtime dependencies, temporary space, and
`/home/maksym/projects`. Protected paths and known credential filename patterns
are denied. The real `luxpower-automation`, `luxpower-telegram-bot`, and this worker
source are read-only. Live controller state, the bot's token/config/database,
the worker's credentials/database, SSH keys, and host process access are denied.

Prepared source copies live under `/home/maksym/projects/codex-workspaces/`.
They exclude `.env`, live config, state, Git metadata, and virtual environments.
They are initial snapshots, not automatically synchronized mirrors. Agent tasks
can implement and test changes there, then report what is ready for review and
deployment. This avoids edits taking effect in the next scheduled controller run.
Existing services are never automatically redeployed by the worker. New projects
may be created under `projects`; supplying secrets and managing services are
separate actions. General task scheduling/service management is not enabled in
this first bridge: system configuration and its bus are outside the sandbox.

The network proxy allows selected documentation/package hosts in
`deploy/codex-config.toml`. Local/private targets and unrelated destinations are
not allowed. Native live web search is enabled. Provider/model access, quotas,
and external account permissions still depend on the saved ChatGPT login.

The worker has its own Codex config/history under
`~/.local/share/codex-telegram/codex` and initially references the existing CLI
authentication file through a private symlink. Only the harness reads it; agent
tools cannot. Do not pass secrets in Telegram. The frontend rejects obvious key
and password patterns, but that heuristic is not comprehensive secret detection.
Never treat chat confirmation as an OS authentication grant.

Filesystem profiles are beta. Actual installed sandbox probes verified project
writes, denied credential/state opens (without reading contents), denied writes
to live code, and isolation from host processes. These controls do not protect
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
