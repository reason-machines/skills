# Where each platform keeps things

Read this in step 2 for the platform you are leaving. Paths change between
versions, so treat each one as a place to look first, check that it exists,
and search nearby when it doesn't. Copy secrets only into `secrets.env`.

## Grok Bot (Cursor, `com.anysphere.sand`)

Checked on 0.78 (2026-11-02). Bots run in a cloud box, and the platform keeps
most state on its own servers, not in the box.

- **Bots:** every bot the person has, with name and description (its
  persona). As a bot you see your own instructions. For the others, use the
  roster your platform tools show you, or ask each bot by message for its
  export (step 2). The desktop app caches the roster in
  `~/Library/Application Support/Grok Bot/sand-client-persistence/*.blob`
  (file names are base32 of keys such as `…roster.last-roster`, JSON inside).
- **Instructions and memory:** not in the box files. Each bot holds its
  memory on the platform, so each bot has to write out what it remembers
  itself: user facts, preferences, project notes, contacts. Ask every bot for
  "everything you remember about me and your work, as Markdown notes", and
  put each one's notes in `memory/reasons/<key>/`. Messages between bots get
  cut off when they are long, so ask for the export in numbered parts
  ("part 1 of N") and check that the last part arrived before you build the
  bundle.
- **Routines:** stored as automations, each with a prompt and a cron
  schedule, and sometimes an event trigger, such as a merged PR in a GitHub
  repository. A bot can list its own, and the router bot can list every bot's.
  Map each automation to a Loop on that bot's Reason, with event triggers in
  `wake_on`. Routines that keep a state file in the box, such as a baseline
  or a seen-list, start fresh in Reason unless you copy that file into the
  bot's memory.
- **Skills:** the person's own skills, in the account skill library shared by
  all bots (each bot's own skill-save tool writes to it; ask a bot to list it,
  or read the library folder in the box). Leave the platform's built-in skills
  out. Migrate the person's own.
- **Connectors:** OAuth apps such as Gmail, Google Calendar, Google Drive,
  Slack, PostHog, GitHub and X. They are not in the box, so list them as
  `connectors`. A person signs in to each one in Reason. A Reason connector
  inside Grok Bot is the way back in, so skip it.
- **Secrets:** the bot's secret store, plus env vars in the box. Write down
  names; copy a value only when you can read it. Skip tokens for services
  the person no longer uses.
- **Work in progress:** each bot's open threads, todos and tasks, and its
  latest transcript turns. Summarise them in that bot's `handoff`.

## Claude Code

- Instructions: `~/.claude/CLAUDE.md`, and `CLAUDE.md` / `.claude/CLAUDE.md`
  in each project.
- Memory: `~/.claude/projects/<project>/memory/*.md` (with a `MEMORY.md`
  index).
- Skills: `~/.claude/skills/<name>/SKILL.md` and `.claude/skills/` in
  repositories (those already reach Reason through the repository).
- Commands and subagents: `~/.claude/commands/*.md`, `~/.claude/agents/*.md`.
  Make a command a skill, and a subagent with its own persona a Reason.
- MCP servers: `mcpServers` in `~/.claude.json` (user and per project) and in
  `.mcp.json`.
- MCP OAuth tokens: macOS Keychain service `Claude Code-credentials`, key
  `mcpOAuth` (one entry per server with its access token, and sometimes a
  refresh token, client id and issuer). `collect_credentials.py --sources
  claude` reads these.
- Schedules: Claude Code has none of its own. Look for cron, launchd or CI
  jobs that run `claude -p`.

## Codex

- Instructions: `~/.codex/AGENTS.md`, `AGENTS.md` in repositories.
- Skills: `~/.codex/skills/<name>/SKILL.md`.
- MCP servers: `[mcp_servers.<name>]` tables in `~/.codex/config.toml`.
- MCP OAuth tokens: macOS Keychain service `Codex MCP Credentials`, one item
  per server (url, issuer, client_id, access and refresh token, expiry).
  `collect_credentials.py --sources codex` reads these.
- Secrets: `env` in those tables and the shell environment.

## Cursor (editor and background agents)

- Rules: `.cursor/rules/*.mdc`, `.cursorrules`, and User Rules in settings.
- MCP servers: `~/.cursor/mcp.json` and `.cursor/mcp.json`.
- Memories: Settings › Rules › Memories. Export as notes.
- MCP OAuth tokens and connector tokens are kept server-side (not on the
  machine), so they can't be collected; sign in to those once in Reason.
- Background agents: their environment secrets live in the Cursor dashboard;
  names only.

Also used by mcp-remote clients: `~/.mcp-auth/**/<hash>_tokens.json` with a
`<hash>_client_info.json` beside it. `collect_credentials.py --sources
mcp-remote` reads these.

## OpenClaw and similar self-hosted assistants

- The workspace directory (often `~/.openclaw/workspace/`) holds `AGENTS.md`,
  `SOUL.md`, `USER.md`, `MEMORY.md` and `memory/*.md`. AGENTS.md and SOUL.md
  together become the Reason's instructions. MEMORY.md and memory/ go to its
  memory folder.
- Skills: `skills/<name>/SKILL.md` in the workspace or the install.
- Schedules: the assistant's cron or heartbeat jobs (its CLI lists them).
- Channels (Telegram, WhatsApp, Discord…) are connectors to set up again.

## Devin

- Knowledge entries become memory notes. Playbooks become skills.
- Secrets live in its settings; names only, unless the person hands over values.
- Scheduled sessions become Loops.
