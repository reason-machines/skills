---
name: migrate-to-reason
metadata:
  version: "2.2.0"
description: Moves a person's agents from another agent platform (Grok Bot, Claude Code, Codex, Cursor, OpenClaw, Devin and similar) into Reason (reasonmachines.com). It first detects which runtime it is running in and where that platform keeps its data, then the agent running it exports everything it can reach (each agent's instructions, memory, skills, routines, secrets, MCP servers and connectors, and the work in progress) into a portable bundle, optionally backs that bundle up to a private GitHub repository, then imports it through Reason's public API. Each agent becomes a Reason, each routine a Loop, memory goes to Reason memory, skills to Reason skills, secrets to Reason secrets and MCP servers to Reason MCP servers. Each Reason is then told what it was doing so it can carry on. Use when asked to migrate, move, port, switch or restore agents into Reason, or to back up agents so they can move later.
---

# Migrate to Reason

You are running inside the platform the person is leaving (or on their
machine next to it). It could be Claude Code, Codex, Cursor, Grok Bot, a
hosted assistant, or something not seen before — the skill works from any of
them, so don't assume; step 1 is to work out what you are. Your job is to move
as much of their setup into Reason as the two platforms allow, prove what
arrived, and name everything that could not move along with the one action
that moves it. Don't stop at a plan: export, import, verify and report in one
go, whatever platform you run from.

**What maps to what**

| Source platform | Reason | How |
| --- | --- | --- |
| An agent/bot (name, persona, instructions) | a Reason | `POST /reasons`, then `PATCH` for repositories and model |
| Its routines, schedules, cron jobs, triggers | Loops on that Reason | `POST /reasons/{id}/loops` (`schedule`, cron or `every 30 minutes`) |
| Memory files, notes, learned facts | that Reason's memory folder, or shared memory | `PUT /memory/file`; the Reason reads its folder at the start of every Session |
| Skills / rules / custom commands | Reason skills | `POST /skills` (one document; small text files are inlined) |
| Secrets, API keys, env vars | workspace secrets | `POST /secrets` |
| MCP servers (url, command, headers) | Reason MCP servers | `POST /mcp-servers` |
| OAuth apps (Slack, GitHub, Linear, Gmail, Notion…) | Reason connections | reported with a reconnect link: a person signs in once |
| Work in progress, open threads | a message to each Reason | `POST /reasons/{id}/messages` (only with `--handoff`) |

Conversation transcripts cannot be imported as history. Their gist goes into
the handoff message and, where it is lasting, into memory.

## 1. Know what you're running in

Before anything, work out which runtime you are and where it keeps things, so
the inventory reads real paths instead of guessing. You already know your own
identity from your system prompt — that is the authority. Corroborate and
ground it with `python3 scripts/detect_runtime.py`: it names the current
runtime and where that platform stores instructions, memory, skills, MCP
servers and tokens, and lists any other agents' config on this machine so a
migration can cover them too.

If it comes back `unknown`, don't stop: trust what your system prompt says you
are and use the generic inventory in
[platform notes](references/platform-notes.md). The skill must execute in full
from any platform; an unrecognised name is never a reason to refuse.

**Done:** you know the runtime (or have accepted it as unknown and will use the
generic inventory), and where its data lives.

## 2. Get a Reason API key

Use `REASON_API_KEY` if it is already set, or the person's password manager or
your platform's secret store if it holds one. Otherwise ask the person, once,
to create one: **reasonmachines.com › Settings › API keys › Create key**, in
the workspace they are moving into, with the scopes `run`, `sessions:read`,
`repos:read`, `memory:read`, `memory:write`, `skills:read`, `skills:write`,
`secrets:read`, `secrets:write`, `mcp:read`, `mcp:write`, `automations:read`
and `automations:write`. Ask them to give it a short expiry. Never print,
log, commit or echo the key, and never put it inside the bundle.

Run `python3 scripts/reason_import.py whoami`. It prints the workspace and
the scopes the key carries, and lists the import steps a missing scope will skip.

**Done:** `whoami` names the intended workspace, and every missing scope is
either granted or accepted as a known skip.

## 3. Inventory the source platform

Find everything there is to move, not only what you happen to know about.
Look at your own configuration, files and tools first, then ask the platform
itself (list agents, list routines, list connected apps). Read
[platform notes](references/platform-notes.md) for where Grok Bot, Claude
Code, Codex, Cursor, OpenClaw and Devin keep each item. For an unknown
platform, check the same kinds of places: config directories, `*.md` rules
and memory, `skills/`, `mcp.json`, cron or schedule definitions, `.env`
files, and the platform's own settings API.

When you are one agent among several (a router or "chief" bot), cover every
agent the person has, not only yourself. When you can't read another agent's
private store, ask it for its export by message, or tell the person which
agent to run this skill in next.

Some connectors can move without a person re-signing in: a few clients keep
each MCP server's OAuth token on this machine. List what is here with
`python3 scripts/collect_credentials.py list BUNDLE` (Codex and Claude Code in
the macOS Keychain, mcp-remote in `~/.mcp-auth`; it shows servers and expiry,
never a token value). Grok Bot, Cursor and hosted assistants keep their tokens
server-side, so those still need a one-time sign-in in Reason.

**Done:** a list of every agent with its instructions, memory, skills,
routines, secrets (names only), MCP servers, connectors and current work, and
each item marked readable or not readable from here.

## 4. Write the bundle

Write the bundle described in [bundle format](references/bundle-format.md):
`manifest.json` plus `reasons/`, `memory/`, `skills/` and `secrets.env`. Copy
content as it is. Don't summarise memory or skills, but do remove anything
that only made sense on the old platform (its tool names and paths) or say
next to it what replaces it. Put a secret value only in `secrets.env`, which
never leaves this machine or box and never goes into git or a memory note.
Reason refuses memory text that contains a secret.

For the local MCP OAuth tokens found in step 3, run
`python3 scripts/collect_credentials.py collect BUNDLE` (add `--mode transfer`
to hand Reason the refresh token and client so it keeps the connection alive,
which signs the old client out of that server; the default `temporary` copies
only the access token and leaves the old client working). It writes an `oauth`
MCP server entry per login into the manifest and the token values into
`secrets.env`; the importer turns each into a connected server, so no sign-in
is needed for those.

Optional backup (the "chief agent backs up every night" pattern): when the
person wants it, or the platform is going away, push the bundle **without
`secrets.env`** to a private GitHub repository they own, and write secret
names (no values) to `secrets.names.txt` there. To restore later, check out
the repo, put `secrets.env` back from the password manager, and run step 5.

**Done:** `python3 scripts/reason_import.py plan BUNDLE` parses the bundle and
lists every item it would create, update or skip, with no parse errors.

## 5. Import

```sh
python3 scripts/reason_import.py plan  BUNDLE              # dry run, no writes
python3 scripts/reason_import.py apply BUNDLE              # write everything
python3 scripts/reason_import.py apply BUNDLE --handoff    # also message each Reason its current work
python3 scripts/reason_import.py undo  BUNDLE [--apply]    # delete what this import created (test runs)
```

Everything lands in the workspace: memory, skills, secrets and MCP servers
belong to the workspace, not to one person, so every member's Sessions can
use them. Tell the person before you import something private, and leave it
out when the workspace has other members who shouldn't see it.

`apply` is safe to re-run. It records what it creates in
`BUNDLE/.reason-import-state.json` and updates only those items next time.
It leaves a same-named Reason, skill, secret or MCP server that someone made
by hand untouched and reports it. Rename the item in the bundle to import it
alongside the existing one, or pass `--overwrite` when the person wants it
replaced. Memory notes are appended without overwriting what is there. A refused item does not stop the run.
Read its error, fix the bundle (for example shorten a note over 32 KiB or split
it, or rename a secret that clashes with a reserved name) and run `apply`
again. `--handoff` starts paid agent work in each Reason that has a `handoff`
text, so use it when the person wants their agents to carry on, which is the
usual case for a migration. The handoff lets Reason fall back to a hosted
model when the person's own model account is out of credit. When the old
platform keeps running for now, import Loops with `"enabled": false` so the
same routine doesn't run twice, and say so in the report.

**Done:** `apply` ends with `failed: 0`, or every failure is fixed or listed
for the report.

## 6. Verify and report

Run `python3 scripts/reason_import.py verify BUNDLE`. It reads back every item
from Reason and compares it with the bundle. Then tell the person, briefly:

- what moved: counts per kind, and a link to each Reason
  (`https://reasonmachines.com/` › Reasons);
- what still needs them, one line each, with the exact place to do it, usually
  `Settings › Connections` for each OAuth connector (Slack, GitHub, Google…)
  and any secret you could not read;
- what could not move at all, and why.

Don't call it done while `verify` reports a mismatch you have not explained.

**Done:** `verify` passes, or each mismatch is in the report with its cause, and
the person has the remaining one-time actions as a short list.
