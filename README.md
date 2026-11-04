# Reason skills

Shared [Reason](https://reasonmachines.com) agent skills. Point a coding agent
at this repo and have it install the skill it needs.

## Install

Each skill is a folder with a `SKILL.md`. Clone once, then copy the folders
you want into your agent's skills directory. Install `work-with-reason` and
`drive-reason` together, side by side: `work-with-reason` runs
`drive-reason`'s helper.

```sh
git clone --depth 1 https://github.com/reason-machines/skills /tmp/reason-skills
```

| Agent | Install |
| --- | --- |
| Claude Code | `mkdir -p ~/.claude/skills && cp -r /tmp/reason-skills/{drive-reason,work-with-reason} ~/.claude/skills/` |
| Codex | `mkdir -p ~/.agents/skills && cp -r /tmp/reason-skills/{drive-reason,work-with-reason} ~/.agents/skills/` |
| Cursor | `mkdir -p ~/.cursor/skills && cp -r /tmp/reason-skills/{drive-reason,work-with-reason} ~/.cursor/skills/` |
| Pi | `mkdir -p ~/.agents/skills && cp -r /tmp/reason-skills/{drive-reason,work-with-reason} ~/.agents/skills/` |

Swap in `migrate-to-reason` (or any other folder) the same way. To update,
pull the clone and copy again.

## work-with-reason

Hands your work to a Reason instead of doing it in a local agent session, so
your team sees it and can continue it. The local agent sends the task to the
right Reason, follows that Session with a background watcher until the done
criteria are verified (steering and unblocking it on the way), puts standing
work in Reason Loops instead of local cron, and keeps what should be
remembered in Reason memory.

- Read [`work-with-reason/SKILL.md`](work-with-reason/SKILL.md).
- Your own setup (which workspace, which Reason owns which area, whether this
  is your default for all work) goes in your `AGENTS.md` or `CLAUDE.md`, not
  in the skill.

## drive-reason

Drives Reason through its public `/v3` HTTP API with one Bun helper,
[`drive-reason/scripts/reason.ts`](drive-reason/scripts/reason.ts): Reasons,
their messages and settings, Loops (schedule, webhook, GitHub and Slack
wakes), personal and team memory, Sessions and their check-ins.

- Read [`drive-reason/SKILL.md`](drive-reason/SKILL.md).
- Needs [Bun](https://bun.sh) and a Reason API key (Settings › API) in
  `REASON_API_KEY`. The helper reads the workspace from `/v3/self`; set
  `REASON_WORKSPACE_ID` to pin it. Tests and probes use a separate test
  workspace through `REASON_TESTS_API_KEY` and `--tests`.
- Offline tests: `bun test drive-reason/scripts/reason.test.ts`.

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
