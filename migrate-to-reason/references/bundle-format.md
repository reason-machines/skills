# Bundle format

Read this when writing a bundle in step 3. A bundle is a directory:

```text
BUNDLE/
  manifest.json            what to create (required)
  reasons/<key>/instructions.md   long instructions, one file per agent (optional)
  memory/reasons/<key>/<note>.md  that agent's memory, into its Reason's folder
  memory/<note>.md         shared memory for the person or workspace
  skills/<name>/SKILL.md   one directory per skill, plus small text files
  secrets.env              NAME=value lines; never committed, never printed
  .reason-import-state.json  written by the importer; IDs it created
```

## manifest.json

```json
{
  "source": "grok-bot",
  "reasons": [
    {
      "key": "gtm",
      "name": "GTM",
      "instructions": "inline text, or omit and use instructions_file",
      "instructions_file": "reasons/gtm/instructions.md",
      "default_repositories": ["owner/repo"],
      "default_model": null,
      "loops": [
        {
          "name": "Morning digest",
          "prompt": "What to do on each wake, written so it stands alone.",
          "schedule": "0 9 * * 1-5",
          "timezone": "America/Los_Angeles",
          "until": null,
          "max_runs": null
        }
      ],
      "handoff": "What this agent was doing, what is open, what to do next."
    }
  ],
  "mcp_servers": [
    {
      "name": "Linear",
      "transport": "http",
      "url": "https://mcp.linear.app/mcp",
      "auth_type": "bearer",
      "secret_headers": { "Authorization": "LINEAR_TOKEN" }
    },
    {
      "name": "filesystem",
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
      "secret_env": { "GITHUB_TOKEN": "GITHUB_TOKEN" }
    },
    {
      "name": "PandaDoc",
      "transport": "http",
      "url": "https://mcp.pandadoc.com/v1/mcp",
      "auth_type": "oauth",
      "oauth": {
        "source_application": "codex",
        "mode": "temporary",
        "access_token_secret": "MCP_OAUTH_PANDADOC_ACCESS_TOKEN",
        "refresh_token_secret": "MCP_OAUTH_PANDADOC_REFRESH_TOKEN",
        "scopes": "read write",
        "expires_at": "2027-10-03T00:20:40Z",
        "client": { "client_id": "…", "token_endpoint_auth_method": "none" }
      }
    }
  ],
  "connectors": [
    { "name": "Slack", "note": "OAuth app; reconnect in Settings › Connections" }
  ]
}
```

Field rules:

- `key`: a short stable id for the agent, lower case. Its memory goes under
  `memory/reasons/<key>/`.
- `name`: at most 80 characters and unique in the bundle. A Reason this
  import created earlier is updated. One with the same name that someone else
  made is left alone (see `--overwrite`).
- `instructions`: at most 16,000 characters. Put a longer persona's lasting
  facts in memory instead and keep the instructions to who the agent is and
  how it works.
- `default_repositories`: up to 6 connected `owner/name` repositories. A
  repository the workspace has not connected is refused; list it in the
  report.
- `default_model`: leave `null` unless you know a Reason model id
  (`GET /v3/organizations/{org}/models`).
- `loops[].schedule`: a cron expression, `every N minutes|hours`, or a
  one-time ISO time. Write `prompt` so it makes sense with no conversation
  around it.
- `loops[].wake_on`: use this instead of `schedule` for anything else that
  wakes the routine, as Reason's list of up to 8 wakes, for example
  `[{"schedule": "25 4 * * *"}, {"github": {"repo": "owner/name",
  "event": "pr_closed"}}]`, or `[{"webhook": {}}]`. The repository must be
  connected to the workspace first (Settings › Repositories), or the wake is
  refused and only the schedule is kept. `pr_closed` fires for merged and
  unmerged closes alike, so if the routine only cares about merges, say so in
  the `prompt`. A Loop with more than one kind of wake is in early access in
  some workspaces, which refuse it with 403 `wake_on_unavailable`; then import
  it as two Loops. Read the `wake_on` schema in
  `https://docs.reasonmachines.com/openapi.json` (`createReasonLoop`) for the
  exact GitHub and Slack fields. A webhook wake gets a new URL, so point the
  sender at it after import.
- `handoff`: sent as the first message to the Reason only with `--handoff`.
- `mcp_servers[].secret_headers` / `secret_env`: map a header or env name to a
  name in `secrets.env`. The importer sends the value as a write-only MCP
  server secret, so it is never stored in plain headers.
- `mcp_servers[].oauth`: an OAuth login carried over from a local client (see
  the PandaDoc example and `scripts/collect_credentials.py`, which writes these
  for you). The importer creates the server and then hands Reason the token
  through its `oauth-import` endpoint, so the connector works without a
  sign-in. `access_token_secret` (and, for `mode: "transfer"`,
  `refresh_token_secret` and `client.client_secret_secret`) name entries in
  `secrets.env`; those `MCP_OAUTH_*` values feed the import only and are never
  stored as workspace secrets. `transfer` keeps the connection alive but signs
  the old client out of that server; `temporary` copies only the access token
  and expires with it. A token the server rejects (usually expired) falls back
  to a one-time sign-in, reported in the run.
- `connectors`: OAuth apps a local client does not hold a token for, so a
  person signs in once in Reason. They are listed in the report, not created.
- `loops[].enabled`: set `false` to import a Loop paused, for example while
  testing a migration or when the old platform keeps running in parallel.

## memory/

Each `*.md` file up to 32 KiB becomes one note (a longer one is split at its
headings). Put each agent's own memory under `memory/reasons/<key>/`, using the
`key` from the manifest. It goes to that Reason's memory folder
(`reasons/<first 12 letters and digits of the Reason id>/`), and every Session
of that Reason reads that folder's `memory.md` index at its start. Reason
keeps that index up to date on its own.

Anything else in `memory/` is workspace memory. It holds only top-level
notes, so `a/b.md` becomes `a-b.md`. Paths are made lower
case and get `.md`. Reason builds and maintains the index itself (the
workspace `MEMORY.md` and each folder's `memory.md`) as notes are written, so
a `memory/MEMORY.md` in the bundle is not imported; it was only an index.

Memory that is a secret is refused, so leave keys, passwords and tokens out of
notes; they go in `secrets.env`.

## skills/

`skills/<name>/SKILL.md` with `name` and `description` frontmatter. The body
becomes the skill's instructions. Other text files in the directory (under
64 KiB in total) are appended to the instructions as `## File: <path>`
sections, because a Reason skill is a single document. Larger or binary files
are listed as not migrated. To keep them, push the skill directory to
`.agents/skills/<name>/` in a repository the Reason uses, and Reason picks it
up from there.

## secrets.env

```text
# NAME=value, one per line; quotes optional
OPENAI_API_KEY=sk-...
LINEAR_TOKEN="lin_api_..."
```

Names: letters, digits and underscores, starting with a letter. Names the
runtime owns (`PATH`, `HOME`, `USER`…) are refused. The importer never
prints values. A name starting `MCP_OAUTH_` is reserved for an `mcp_servers`
`oauth` block: it feeds that server's `oauth-import` and is never stored as a
workspace secret. `secrets.env` holds live tokens, so it never goes into git
or a memory note.
