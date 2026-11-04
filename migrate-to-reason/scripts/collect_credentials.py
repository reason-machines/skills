#!/usr/bin/env python3
"""Collect the MCP OAuth logins a local agent client already holds, into a migrate-to-reason bundle.

Usage:
  collect_credentials.py list   BUNDLE [--sources codex,claude,mcp-remote]
  collect_credentials.py collect BUNDLE [--sources ...] [--mode temporary|transfer]
                                        [--only NAME[,NAME]] [--skip NAME[,NAME]]

Some coding agents keep each MCP server's OAuth token on this machine (Codex and Claude Code in the
macOS login Keychain, mcp-remote in ~/.mcp-auth). This reads those, so the migration can re-import
them into Reason through its oauth-import endpoint instead of a person re-signing in to each service.
`list` only inventories what exists (server, URL, expiry, whether a refresh token and client are
present) and never reads a token value. `collect` adds an `oauth` MCP server entry to the bundle's
manifest.json for each login and writes the token values into secrets.env; it never prints a value.

Platforms that keep their connector tokens server-side (Grok Bot / Cursor, hosted assistants) have
nothing to collect here; a person signs in to those once in Reason. The importer turns each entry
into a server plus an oauth-import call (see reason_import.py and references/bundle-format.md).

--mode temporary (default) copies only the access token: the source client keeps working and the
connection lasts until that token expires. --mode transfer also hands Reason the refresh token and
the registered client so Reason keeps it alive; a server that rotates refresh tokens then signs the
source client out, so use it on a real cutover. Reading the Keychain can raise a macOS access prompt.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

KEYCHAIN_TIMEOUT = 20


def run(cmd: list[str], timeout: int = KEYCHAIN_TIMEOUT) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def keychain_accounts(service: str) -> list[str]:
    """Account names of every login Keychain item for one service (macOS only)."""
    if sys.platform != "darwin":
        return []
    # dump-keychain is the only way to enumerate items for a service; filter to this one service.
    out = run(["security", "dump-keychain"], timeout=40) or ""
    # Each item prints its attributes together; "acct" appears before "svce". Pair the most recent
    # account with the service line that follows it, keeping only this one service's items.
    accts, last_acct = [], None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith('"acct"'):
            m = re.search(r'="(.*)"', s)
            last_acct = m.group(1) if m else None
        elif s.startswith('"svce"'):
            if (f'="{service}"' in s) and last_acct is not None:
                accts.append(last_acct)
            last_acct = None
    return sorted(set(accts))


def keychain_value(service: str, account: str) -> str | None:
    if sys.platform != "darwin":
        return None
    args = ["security", "find-generic-password", "-s", service, "-w"]
    if account:
        args[4:4] = ["-a", account]
    return run(args)


def iso(expires_at_ms: float | None, expires_in_s: float | None = None, issued_ms: float | None = None) -> str | None:
    t = None
    if expires_at_ms:
        t = expires_at_ms / 1000 if expires_at_ms > 1e12 else expires_at_ms
    elif expires_in_s:
        t = (issued_ms / 1000 if issued_ms else time.time()) + expires_in_s
    if not t:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class Login:
    def __init__(self, source: str, name: str, url: str, access: str, *, refresh: str | None = None,
                 client_id: str | None = None, client_secret: str | None = None,
                 auth_method: str | None = None, issuer: str | None = None,
                 scopes: str = "", expires_at: str | None = None):
        self.source, self.name, self.url, self.access = source, name, url, access
        self.refresh, self.client_id, self.client_secret = refresh, client_id, client_secret
        self.auth_method, self.issuer, self.scopes, self.expires_at = auth_method, issuer, scopes, expires_at

    @property
    def slug(self) -> str:
        return re.sub(r"_+", "_", re.sub(r"[^A-Z0-9]+", "_", (self.name or self.url).upper())).strip("_") or "SERVER"


def from_codex() -> list[Login]:
    out = []
    for acct in keychain_accounts("Codex MCP Credentials"):
        raw = keychain_value("Codex MCP Credentials", acct)
        if not raw:
            continue
        try:
            d = json.loads(raw)
            tr = d.get("token_response")
            tr = tr if isinstance(tr, dict) else json.loads(tr or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        access = tr.get("access_token")
        if not access:
            continue
        out.append(Login("codex", d.get("server_name") or acct.split("|")[0], d.get("url", ""), access,
                          refresh=tr.get("refresh_token"), client_id=d.get("client_id"),
                          issuer=d.get("issuer"), scopes=tr.get("scope", ""),
                          expires_at=iso(d.get("expires_at"), tr.get("expires_in"))))
    return out


def from_claude() -> list[Login]:
    raw = keychain_value("Claude Code-credentials", "")
    if not raw:
        return []
    try:
        m = (json.loads(raw).get("mcpOAuth")) or {}
    except json.JSONDecodeError:
        return []
    out = []
    for key, v in m.items():
        if not isinstance(v, dict) or not v.get("accessToken"):
            continue
        out.append(Login("claude", v.get("serverName") or key.split("|")[0], v.get("serverUrl", ""), v["accessToken"],
                          refresh=v.get("refreshToken"), client_id=v.get("clientId"), client_secret=v.get("clientSecret"),
                          issuer=v.get("issuer"), scopes=v.get("scope", ""), expires_at=iso(v.get("expiresAt"))))
    return out


def from_mcp_remote() -> list[Login]:
    root = Path.home() / ".mcp-auth"
    out = []
    for tok in glob.glob(str(root / "**" / "*_tokens.json"), recursive=True):
        try:
            t = json.loads(Path(tok).read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not t.get("access_token"):
            continue
        base = tok[: -len("_tokens.json")]
        ci = {}
        for suff in ("_client_info.json", "_client_info.json"):
            p = base + suff
            if os.path.exists(p):
                try:
                    ci = json.loads(Path(p).read_text())
                except (json.JSONDecodeError, OSError):
                    ci = {}
        url = ci.get("server_url") or ci.get("serverUrl") or ""
        name = ci.get("client_name") or (re.sub(r"^https?://", "", url).split("/")[0] if url else Path(base).name)
        issued = os.path.getmtime(tok) * 1000
        out.append(Login("mcp-remote", name, url, t["access_token"], refresh=t.get("refresh_token"),
                         client_id=ci.get("client_id"), client_secret=ci.get("client_secret"),
                         scopes=t.get("scope", ""), expires_at=iso(None, t.get("expires_in"), issued)))
    return out


SOURCES = {"codex": from_codex, "claude": from_claude, "mcp-remote": from_mcp_remote}


def gather(sources: list[str]) -> list[Login]:
    out, seen = [], set()
    for src in sources:
        for lg in SOURCES[src]():
            key = (lg.name.strip().lower(), lg.url)
            if key in seen:
                continue
            seen.add(key)
            out.append(lg)
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("cmd", choices=["list", "collect"])
    ap.add_argument("bundle")
    ap.add_argument("--sources", default="codex,claude,mcp-remote")
    ap.add_argument("--mode", choices=["temporary", "transfer"], default="temporary")
    ap.add_argument("--only", default="")
    ap.add_argument("--skip", default="")
    a = ap.parse_args(argv)
    srcs = [s.strip() for s in a.sources.split(",") if s.strip() in SOURCES]
    only = {x.strip().lower() for x in a.only.split(",") if x.strip()}
    skip = {x.strip().lower() for x in a.skip.split(",") if x.strip()}

    logins = gather(srcs)
    if only:
        logins = [l for l in logins if l.name.strip().lower() in only]
    if skip:
        logins = [l for l in logins if l.name.strip().lower() not in skip]

    if not logins:
        print("No local MCP OAuth logins found for: " + ", ".join(srcs))
        print("(Grok Bot/Cursor and hosted assistants keep tokens server-side; sign in to those once in Reason.)")
        return 0

    for l in logins:
        exp = l.expires_at or "no expiry recorded"
        print(f"  {l.source:10} {l.name:28} {l.url or '(no url)'}  | refresh: {'yes' if l.refresh else 'no'} | client: {'yes' if l.client_id else 'no'} | expires: {exp}")

    if a.cmd == "list":
        print(f"\n{len(logins)} login(s). Run `collect` to add them to the bundle (--mode transfer to hand over refresh tokens).")
        return 0

    root = Path(a.bundle)
    mf_path = root / "manifest.json"
    if not mf_path.exists():
        sys.exit(f"{mf_path} not found; make the bundle first (see references/bundle-format.md)")
    mf = json.loads(mf_path.read_text())
    servers = mf.setdefault("mcp_servers", [])
    by_name = {s.get("name", "").strip().lower(): s for s in servers}
    env_path = root / "secrets.env"
    env_lines: list[str] = []
    added = 0
    for l in logins:
        if not l.url:
            print(f"  skip {l.name}: no server URL recorded, can't import")
            continue
        slug = l.slug
        oauth = {"source_application": l.source, "mode": a.mode,
                 "access_token_secret": f"MCP_OAUTH_{slug}_ACCESS_TOKEN",
                 "scopes": l.scopes or ""}
        if l.expires_at:
            oauth["expires_at"] = l.expires_at
        env_lines.append(f"MCP_OAUTH_{slug}_ACCESS_TOKEN={l.access}")
        if a.mode == "transfer" and l.refresh and l.client_id:
            oauth["refresh_token_secret"] = f"MCP_OAUTH_{slug}_REFRESH_TOKEN"
            env_lines.append(f"MCP_OAUTH_{slug}_REFRESH_TOKEN={l.refresh}")
            client = {"client_id": l.client_id,
                      "token_endpoint_auth_method": l.auth_method or ("client_secret_basic" if l.client_secret else "none")}
            if l.client_secret:
                client["client_secret_secret"] = f"MCP_OAUTH_{slug}_CLIENT_SECRET"
                env_lines.append(f"MCP_OAUTH_{slug}_CLIENT_SECRET={l.client_secret}")
            oauth["client"] = client
        entry = {"name": l.name, "transport": "http", "url": l.url, "auth_type": "oauth", "oauth": oauth}
        cur = by_name.get(l.name.strip().lower())
        if cur:
            cur.update(entry)
        else:
            servers.append(entry)
            by_name[l.name.strip().lower()] = entry
        added += 1

    mf_path.write_text(json.dumps(mf, indent=1))
    # Append secret values; secrets.env is local to the bundle and must never be committed.
    existing = env_path.read_text() if env_path.exists() else ""
    have = {ln.split("=", 1)[0] for ln in existing.splitlines() if "=" in ln and not ln.startswith("#")}
    with env_path.open("a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        for line in env_lines:
            if line.split("=", 1)[0] not in have:
                f.write(line + "\n")
    print(f"\nAdded {added} OAuth MCP server(s) to {mf_path.name} and their tokens to secrets.env (mode: {a.mode}).")
    print("Next: python3 scripts/reason_import.py apply BUNDLE  (the importer creates each server and calls oauth-import).")
    print("secrets.env holds live tokens — never commit it.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
