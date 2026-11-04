#!/usr/bin/env python3
"""Work out which agent runtime this skill is running inside, and where it keeps its data.

Usage: detect_runtime.py [--json]

The skill must run from any platform (Claude Code, Codex, Cursor, Grok Bot, a hosted assistant, or
one not seen before), so before inventorying anything it should know what it is. You, the running
agent, already know your own identity from your system prompt — that is the authority. This only
corroborates it from the environment and names where that platform stores instructions, memory,
skills, MCP servers and tokens, so the inventory step reads real paths instead of guessing. It also
lists any OTHER agents' config present on this machine, so a migration can cover all of them.

If the runtime comes back "unknown", don't stop: trust what your system prompt tells you that you
are, and fall back to the generic inventory in references/platform-notes.md (config dirs, *.md rules
and memory, skills/, mcp.json/config, schedules, .env). The point is to execute fully everywhere,
never to refuse because a name wasn't recognised.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOME = Path.home()


def has_keychain(service: str) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        return subprocess.run(["security", "find-generic-password", "-s", service],
                              capture_output=True, timeout=10).returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# Where each known runtime keeps what a migration needs. Kept short; platform-notes.md has the detail.
WHERE = {
    "claude-code": {
        "instructions": "~/.claude/CLAUDE.md and per-repo CLAUDE.md / .claude/CLAUDE.md",
        "memory": "~/.claude/projects/<project>/memory/*.md",
        "skills": "~/.claude/skills/<name>/SKILL.md; commands ~/.claude/commands/*.md; sub-agents ~/.claude/agents/*.md",
        "mcp_servers": "mcpServers in ~/.claude.json and .mcp.json",
        "mcp_tokens": "macOS Keychain 'Claude Code-credentials' -> mcpOAuth (collect_credentials.py --sources claude)",
        "schedules": "none built in; look for cron/launchd/CI running `claude -p`",
    },
    "codex": {
        "instructions": "~/.codex/AGENTS.md and per-repo AGENTS.md",
        "memory": "none built in; treat notes/docs the agent keeps as memory",
        "skills": "~/.codex/skills/<name>/SKILL.md",
        "mcp_servers": "[mcp_servers.*] in ~/.codex/config.toml",
        "mcp_tokens": "macOS Keychain 'Codex MCP Credentials' (collect_credentials.py --sources codex)",
        "schedules": "none built in",
    },
    "cursor": {
        "instructions": "User Rules; .cursor/rules/*.mdc; .cursorrules",
        "memory": "Settings > Rules > Memories (export as notes)",
        "skills": "n/a",
        "mcp_servers": "~/.cursor/mcp.json and .cursor/mcp.json",
        "mcp_tokens": "kept server-side; sign in once in Reason",
        "schedules": "background agents (dashboard)",
    },
    "grok-bot": {
        "instructions": "each bot's persona/description (ask the bot, or read its box profile)",
        "memory": "per-bot, held by the platform; have each bot export what it remembers",
        "skills": "the account skill library the bots share (ask a bot to list it)",
        "mcp_servers": "the bot's connected MCP servers (list via the platform)",
        "mcp_tokens": "kept server-side; sign in once in Reason",
        "schedules": "automations (prompt + cron, sometimes a GitHub event) per bot",
    },
    "openclaw": {
        "instructions": "~/.openclaw/workspace/AGENTS.md + SOUL.md",
        "memory": "~/.openclaw/workspace/MEMORY.md and memory/*.md",
        "skills": "skills/<name>/SKILL.md in the workspace",
        "mcp_servers": "the assistant's mcp config",
        "mcp_tokens": "mcp-remote ~/.mcp-auth (collect_credentials.py --sources mcp-remote)",
        "schedules": "the assistant's cron/heartbeat jobs",
    },
}


def detect_current() -> tuple[str, str, list[str]]:
    """Return (runtime, confidence, signals) for the runtime this process is in."""
    e = os.environ
    sig = []
    cf = e.get("__CFBundleIdentifier", "")
    if e.get("CLAUDECODE") or e.get("CLAUDE_CODE_ENTRYPOINT") or e.get("CLAUDE_CODE_SESSION_ID"):
        sig = [k for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID") if e.get(k)]
        return "claude-code", "high", sig
    if any(k.startswith("CODEX_") for k in e) or "codex" in e.get("_", "").lower():
        return "codex", "high", [k for k in e if k.startswith("CODEX_")] or ["argv0=codex"]
    if any(k.startswith("SAND_") for k in e) or cf == "com.anysphere.sand" or Path("/home/box/sand-data").exists():
        return "grok-bot", "high", [k for k in e if k.startswith("SAND_")] or ["/home/box/sand-data" if Path("/home/box/sand-data").exists() else cf]
    if any(k.startswith("CURSOR_") for k in e) or "cursor" in cf.lower() or e.get("TERM_PROGRAM", "").lower() == "cursor":
        return "cursor", "medium", [k for k in e if k.startswith("CURSOR_")] or [cf or e.get("TERM_PROGRAM", "")]
    if (HOME / ".openclaw").exists():
        return "openclaw", "low", ["~/.openclaw present (not a runtime signal on its own)"]
    return "unknown", "none", []


def machine_sources() -> list[tuple[str, str]]:
    """Other agents' config present on this machine, each as (runtime, evidence)."""
    found = []
    checks = [
        ("claude-code", HOME / ".claude", "~/.claude"),
        ("codex", HOME / ".codex", "~/.codex"),
        ("cursor", HOME / ".cursor", "~/.cursor"),
        ("openclaw", HOME / ".openclaw", "~/.openclaw"),
        ("mcp-remote", HOME / ".mcp-auth", "~/.mcp-auth"),
    ]
    for name, path, label in checks:
        if path.exists():
            found.append((name, label))
    for name, svc in (("codex", "Codex MCP Credentials"), ("claude-code", "Claude Code-credentials"),
                      ("grok-bot", "Grok Bot Safe Storage"), ("cursor", "Cursor Safe Storage")):
        if has_keychain(svc):
            found.append((name, f"Keychain '{svc}'"))
    for name, app in (("grok-bot", "/Applications/Grok Bot.app"), ("cursor", "/Applications/Cursor.app"),
                      ("claude-code", "/Applications/Claude.app")):
        if Path(app).exists():
            found.append((name, app))
    return found


def main(argv: list[str]) -> int:
    runtime, conf, sig = detect_current()
    sources: dict[str, list[str]] = {}
    for name, ev in machine_sources():
        sources.setdefault(name, []).append(ev)
    where = WHERE.get(runtime)
    if "--json" in argv:
        print(json.dumps({"runtime": runtime, "confidence": conf, "signals": sig,
                          "where": where, "sources_on_machine": sources}, indent=1))
        return 0
    print(f"Current runtime: {runtime} (confidence: {conf})")
    if sig:
        print("  signals: " + ", ".join(sig))
    print("  Your system prompt is the authority on what you are — if it says otherwise, trust it.")
    if where:
        print(f"\nWhere {runtime} keeps what a migration needs:")
        for k, v in where.items():
            print(f"  {k:13} {v}")
    else:
        print("\nRuntime not recognised. Proceed from what you know you are, using the generic")
        print("inventory in references/platform-notes.md (config dirs, *.md rules/memory, skills/,")
        print("mcp config, schedules, .env). Operate in full regardless.")
    if sources:
        print("\nOther agent config present on this machine (a migration can cover these too):")
        for name, evs in sorted(sources.items()):
            print(f"  {name:12} {', '.join(sorted(set(evs)))}")
    print("\nNext: inventory with references/platform-notes.md, then collect_credentials.py for local MCP tokens.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
