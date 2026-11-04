# Reason skills

Shared [Reason](https://reasonmachines.com) agent skills. Point a coding agent
at this repo and have it install the skill it needs.

## migrate-to-reason

Moves a person's agents from another platform (Grok Bot, Claude Code, Codex,
Cursor, OpenClaw, Devin, …) into a Reason workspace. The agent on the old
platform exports what it can reach into a portable bundle, then
`migrate-to-reason/scripts/reason_import.py` writes it through Reason's public
`/v3` API: each agent becomes a Reason, routines become Loops, memory and
skills and secrets become workspace memory/skills/secrets, and MCP servers are
recreated — including carrying over OAuth logins that a local client already
holds (`scripts/collect_credentials.py`), so most connectors need no re-sign-in.

- Read [`migrate-to-reason/SKILL.md`](migrate-to-reason/SKILL.md) to run it.
- [`references/bundle-format.md`](migrate-to-reason/references/bundle-format.md) — the bundle a migration produces.
- [`references/platform-notes.md`](migrate-to-reason/references/platform-notes.md) — where each source platform keeps instructions, memory, skills, routines, secrets and tokens.

You need a Reason API key (Settings › API keys) with the scopes listed in
SKILL.md step 1. The scripts use only the Python standard library.

**A migration bundle holds live tokens** in `secrets.env`; it is git-ignored
here and must never be committed.
