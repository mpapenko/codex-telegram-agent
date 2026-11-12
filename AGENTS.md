# Contributor instructions

- Python 3.13+, standard library runtime, deterministic tests with fake Codex.
- This is an owner-and-extended-user local agent bridge. Never accept a Telegram token or sudo password.
- Keep Codex credentials and runtime state outside the source checkout, with mode 0700/0600.
- Preserve protected-project read-only rules and secret denials; internet/LAN are allowed.
- Group all outside-project writes in one explicit approval per change set; deny native escalation and never replay started jobs.
- Host diagnostics and user-service actions use fixed-argument host_system; never expose raw process environments, arbitrary shells or approval databases.
- The authorized root bridge may propose an exact command to a separate root-owned
  broker. Only the authenticated Telegram frontend can approve it. Never approve
  from this worker, expose codes to the model, or accept passwords.
- Keep the boiler controller independent; do not modify its operational state.
- Run pytest, ruff check, and ruff format --check. Do not publish or push repositories.
