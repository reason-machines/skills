#!/usr/bin/env python3
"""Import a migrate-to-reason bundle into a Reason workspace.

Usage:
  reason_import.py whoami
  reason_import.py plan   BUNDLE
  reason_import.py apply  BUNDLE [--handoff] [--only KIND[,KIND]] [--overwrite]
  reason_import.py verify BUNDLE
  reason_import.py undo   BUNDLE [--apply]   delete what this bundle's import created (dry run without --apply)

Kinds: reasons, loops, memory, skills, secrets, mcp, handoff.
Reads REASON_API_KEY (required) and REASON_API_URL (default
https://api.reasonmachines.com). Standard library only. Never prints secret
values or the key.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

API = os.environ.get("REASON_API_URL", "https://api.reasonmachines.com").rstrip("/")
KINDS = ["reasons", "loops", "memory", "skills", "secrets", "mcp", "handoff"]
NEEDS = {
    "reasons": ["run", "sessions:read"],
    "loops": ["automations:read", "automations:write"],
    "memory": ["memory:read", "memory:write"],
    "skills": ["skills:read", "skills:write"],
    "secrets": ["secrets:read", "secrets:write"],
    "mcp": ["mcp:read", "mcp:write"],
    "handoff": ["run"],
}
SKILL_INLINE_LIMIT = 64 * 1024
NOTE_LIMIT = 32 * 1024
TEXT_EXT = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".py", ".sh", ".ts", ".js", ".csv", ".ini", ".cfg"}


class ApiError(Exception):
    def __init__(self, status: int, body: object):
        self.status, self.body = status, body
        code = body.get("error", {}).get("code") if isinstance(body, dict) and isinstance(body.get("error"), dict) else None
        msg = body.get("error", {}).get("message") if isinstance(body, dict) and isinstance(body.get("error"), dict) else None
        if not msg and isinstance(body, dict):
            msg = body.get("message") or body.get("detail")
        self.code = code or (body.get("code") if isinstance(body, dict) else None)
        super().__init__(f"HTTP {status} {self.code or ''} {msg or str(body)[:300]}".strip())


def key() -> str:
    k = os.environ.get("REASON_API_KEY", "").strip()
    if not k:
        sys.exit("REASON_API_KEY is not set. Create one in Reason: Settings > API keys (see SKILL.md step 1).")
    return k


def call(method: str, path: str, body: object | None = None, query: dict | None = None) -> object:
    url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + key())
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "migrate-to-reason/1.0")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = raw.decode("utf8", "replace")
            if e.code in (429, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            raise ApiError(e.code, parsed) from None
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("unreachable")


def items(method_path: str, query: dict | None = None) -> list:
    out, cursor = [], None
    for _ in range(50):
        q = dict(query or {})
        if cursor:
            q["after"] = cursor
        res = call("GET", method_path, query=q or None)
        if isinstance(res, list):
            return res
        out.extend(res.get("items", []))
        cursor = res.get("end_cursor")
        if not res.get("has_next_page") or not cursor:
            break
    return out


# ---------- bundle ----------

def load_bundle(root: Path) -> dict:
    mf = root / "manifest.json"
    if not mf.exists():
        sys.exit(f"{mf} not found")
    m = json.loads(mf.read_text())
    m.setdefault("source", "import")
    for r in m.get("reasons", []):
        if not r.get("name"):
            sys.exit(f"a reason in manifest.json has no name: {r}")
        r["key"] = re.sub(r"[^a-z0-9-]+", "-", (r.get("key") or r["name"]).lower()).strip("-") or "agent"
        if r.get("instructions_file"):
            r["instructions"] = (root / r["instructions_file"]).read_text()
        if r.get("handoff_file"):
            r["handoff"] = (root / r["handoff_file"]).read_text()
    m["_memory"] = load_memory(root)
    m["_skills"] = load_skills(root)
    m["_secrets"] = load_env(root / "secrets.env")
    return m


def load_env(p: Path) -> dict:
    out = {}
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        if k and v:
            out[k] = v
    return out


def norm_note_path(rel: str) -> str:
    p = rel.replace("\\", "/").strip("/").lower()
    p = re.sub(r"[^a-z0-9._/-]+", "-", p)
    p = re.sub(r"-+", "-", p)
    if not p.endswith(".md"):
        p += ".md"
    segs = [s.strip("-") or "x" for s in p.split("/") if s]
    if len(segs) > 4:
        segs = segs[:3] + ["-".join(segs[3:])]
    return "/".join(segs)


def load_memory(root: Path) -> dict:
    d = root / "memory"
    notes, index_lines, too_big = {}, [], []
    if not d.exists():
        return {"notes": notes, "index": index_lines, "too_big": too_big}
    for f in sorted(d.rglob("*")):
        if not f.is_file() or f.name.startswith("."):
            continue
        rel = f.relative_to(d).as_posix()
        try:
            text = f.read_text()
        except UnicodeDecodeError:
            too_big.append(rel + " (binary)")
            continue
        if f.name.upper() == "MEMORY.MD":
            index_lines.extend(l for l in text.splitlines() if l.strip())
            continue
        if not text.strip():
            continue
        if len(text.encode()) > NOTE_LIMIT:
            # split on headings into parts that fit
            parts, cur = [], ""
            for block in re.split(r"(?m)^(?=#{1,3} )", text):
                if len((cur + block).encode()) > NOTE_LIMIT - 200 and cur:
                    parts.append(cur)
                    cur = ""
                cur += block
            if cur:
                parts.append(cur)
            if any(len(p.encode()) > NOTE_LIMIT for p in parts):
                too_big.append(rel)
                continue
            base = norm_note_path(rel)[:-3]
            for i, ptxt in enumerate(parts, 1):
                notes[f"{base}-part{i}.md"] = ptxt
            continue
        notes[norm_note_path(rel)] = text
    return {"notes": notes, "index": index_lines, "too_big": too_big}


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    fm, body = text[3:end], text[end + 4:].lstrip("\n")
    meta, cur = {}, None
    for line in fm.splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m:
            cur = m.group(1)
            v = m.group(2).strip()
            meta[cur] = v.strip("'\"") if v not in (">", "|", ">-", "|-") else ""
        elif cur and line.startswith((" ", "\t")) and cur == "description":
            meta[cur] = (meta[cur] + " " + line.strip()).strip()
    return meta, body


def load_skills(root: Path) -> list:
    d = root / "skills"
    out = []
    if not d.exists():
        return out
    for sk in sorted(p for p in d.iterdir() if p.is_dir()):
        md = sk / "SKILL.md"
        if not md.exists():
            continue
        meta, body = parse_frontmatter(md.read_text())
        name = meta.get("name") or sk.name
        extra, skipped, used = [], [], 0
        for f in sorted(sk.rglob("*")):
            if not f.is_file() or f == md or f.name.startswith("."):
                continue
            rel = f.relative_to(sk).as_posix()
            if f.suffix.lower() not in TEXT_EXT:
                skipped.append(rel)
                continue
            try:
                t = f.read_text()
            except UnicodeDecodeError:
                skipped.append(rel)
                continue
            if used + len(t.encode()) > SKILL_INLINE_LIMIT:
                skipped.append(rel)
                continue
            used += len(t.encode())
            fence = "````" if "```" in t else "```"
            extra.append(f"\n\n## File: {rel}\n\n{fence}\n{t.rstrip()}\n{fence}\n")
        instructions = body.rstrip() + "".join(extra)
        if extra:
            instructions += "\n\n(Files above were inlined from the original skill directory; read paths in the instructions as these sections.)\n"
        out.append({"name": name, "description": meta.get("description", "")[:1024], "instructions": instructions, "skipped_files": skipped})
    return out


def memory_target(rel: str, scope: str, reason_ids: dict, planning: bool = False) -> str | None:
    """Where a bundle note goes in the workspace's memory.

    memory/reasons/<agent-key>/<note> goes to that Reason's own folder,
    reasons/<first 12 letters/digits of its id>/<note>, whose memory.md index
    every Session of that Reason loads. Any other note is a workspace note;
    workspace memory keeps only top-level notes and place folders, so a/b.md
    becomes a-b.md.
    """
    segs = rel.split("/")
    if len(segs) >= 3 and segs[0] == "reasons":
        rid = reason_ids.get(segs[1])
        if not rid:
            return f"reasons/<new {segs[1]}>/" + "-".join(segs[2:]) if planning else None
        key = re.sub(r"[^a-z0-9]", "", rid.lower())[:12]
        name = "-".join(segs[2:])
        return f"reasons/{key}/{name}"
    return "-".join(segs)


# ---------- state ----------

def load_state(root: Path) -> dict:
    p = root / ".reason-import-state.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(root: Path, st: dict) -> None:
    (root / ".reason-import-state.json").write_text(json.dumps(st, indent=2, sort_keys=True))


# ---------- import ----------

class Run:
    def __init__(self, root: Path, apply: bool, handoff: bool, only: set[str], overwrite: bool = False):
        self.root, self.apply, self.handoff, self.only, self.overwrite = root, apply, handoff, only, overwrite
        self.b = load_bundle(root)
        self.st = load_state(root)
        self.self = call("GET", "/v3/self")
        self.org = self.self["org_id"]
        self.principal = self.self.get("service_user_id") or self.self.get("user_id")
        self.scopes = set(self.self.get("scopes", []))
        self.counts: dict[str, dict[str, int]] = {}
        self.failures: list[str] = []
        self.manual: list[str] = []

    # Only items this import created (recorded in the state file) are updated on a re-run. A same-named item
    # someone made by hand is left alone, so a migration never overwrites existing work unless --overwrite.
    def owned(self, kind: str) -> list:
        return self.st.setdefault("created", {}).setdefault(kind, [])

    def mine(self, kind: str, item_id: str) -> bool:
        return self.overwrite or item_id in self.owned(kind)

    def created(self, kind: str, item_id: str | None) -> None:
        if item_id and item_id not in self.owned(kind):
            self.owned(kind).append(item_id)
            if self.apply:
                save_state(self.root, self.st)

    def foreign(self, kind: str, name: str) -> None:
        self.note(kind, "skip", name, "already exists and was not made by this import; left unchanged")
        self.manual.append(f"{kind} {name}: one with this name already exists in the workspace and was left as it is. Rename it in the bundle to import it alongside, or re-run with --overwrite to replace it")

    def p(self, path: str) -> str:
        return f"/v3/organizations/{self.org}{path}"

    def want(self, kind: str) -> bool:
        if self.only and kind not in self.only:
            return False
        if kind == "handoff" and not self.handoff:
            return False
        missing = [s for s in NEEDS[kind] if s not in self.scopes]
        if missing:
            self.manual.append(f"{kind}: skipped, the API key lacks {', '.join(missing)}")
            return False
        return True

    def note(self, kind: str, action: str, label: str, detail: str = "") -> None:
        c = self.counts.setdefault(kind, {})
        c[action] = c.get(action, 0) + 1
        verb = action if self.apply else f"would {action}"
        print(f"  {kind:8} {verb:14} {label}" + (f"  ({detail})" if detail else ""))

    def fail(self, kind: str, label: str, err: Exception) -> None:
        self.counts.setdefault(kind, {}).setdefault("failed", 0)
        self.counts[kind]["failed"] += 1
        self.failures.append(f"{kind} {label}: {err}")
        print(f"  {kind:8} FAILED         {label}  ({err})")

    # reasons + loops + handoff
    def reasons(self) -> None:
        defs = self.b.get("reasons", [])
        if not defs:
            return
        do_r, do_l, do_h = self.want("reasons"), self.want("loops"), self.want("handoff")
        if not (do_r or do_l or do_h):
            return
        existing = {r["name"].strip().lower(): r for r in items(self.p("/reasons")) if r.get("mine", True)}
        ids = self.st.setdefault("reasons", {})
        for r in defs:
            label = r["name"]
            rid = ids.get(r["key"])
            match = existing.get(label.strip().lower())
            if not rid and match:
                if self.mine("reasons", match["id"]) or not do_r:
                    rid = match["id"]
                else:
                    self.foreign("reasons", label)
                    continue
            instr = (r.get("instructions") or "")[:16000]
            if do_r:
                try:
                    patch = {"instructions": instr} if instr else {}
                    if r.get("default_repositories") is not None:
                        patch["default_repositories"] = r["default_repositories"][:6]
                    if r.get("default_model"):
                        patch["default_model"] = r["default_model"]
                    if rid and r["key"] in ids and ids[r["key"]] == rid and rid not in self.owned("reasons") and not self.overwrite:
                        self.created("reasons", rid)  # recorded by an older importer version
                    if rid:
                        self.note("reasons", "update", label)
                        if self.apply and patch:
                            call("PATCH", self.p(f"/reasons/{rid}"), patch)
                    else:
                        self.note("reasons", "create", label)
                        if self.apply:
                            body = {"name": label[:80]}
                            if instr:
                                body["instructions"] = instr
                            created = call("POST", self.p("/reasons"), body)
                            rid = created.get("id") or created.get("reason", {}).get("id")
                            ids[r["key"]] = rid
                            self.created("reasons", rid)
                            save_state(self.root, self.st)
                            rest = {k: v for k, v in patch.items() if k != "instructions"}
                            if rest:
                                try:
                                    call("PATCH", self.p(f"/reasons/{rid}"), rest)
                                except ApiError as e:
                                    self.fail("reasons", f"{label} defaults", e)
                    if len(r.get("instructions") or "") > 16000:
                        self.manual.append(f"{label}: instructions cut to 16,000 characters; move the rest into memory")
                except ApiError as e:
                    self.fail("reasons", label, e)
                    continue
            if rid:
                ids[r["key"]] = rid
            if do_l and r.get("loops"):
                self.loops(r, rid)
            if do_h and r.get("handoff"):
                self.send_handoff(r, rid)
        if self.apply:
            save_state(self.root, self.st)

    def loops(self, r: dict, rid: str | None) -> None:
        have = {}
        if rid:
            try:
                have = {l["name"].strip().lower(): l for l in items(self.p(f"/reasons/{rid}/loops")) if l.get("name")}
            except ApiError as e:
                self.fail("loops", r["name"], e)
                return
        for l in r["loops"]:
            name = l.get("name") or l["prompt"][:60]
            label = f"{r['name']} / {name}"
            wake = l.get("wake_on") or ([{"schedule": l["schedule"]}] if l.get("schedule") else None)
            if not wake:
                self.fail("loops", label, RuntimeError("give it a schedule or a wake_on list"))
                continue
            when = l.get("schedule") or ", ".join(next(iter(w)) for w in wake)
            # Fields the API accepts alongside wake_on, and those it refuses with it (set in their own call).
            core = {"name": name, "prompt": l["prompt"]}
            for k in ("until", "max_runs", "model", "notification_policy"):
                if l.get(k) is not None:
                    core[k] = l[k]
            extra = {k: l[k] for k in ("enabled", "timezone") if l.get(k) is not None}
            schedule_only = [w for w in wake if "schedule" in w]
            cur = have.get(name.strip().lower())
            try:
                if cur:
                    lid = cur["id"]
                    self.note("loops", "update", label, when)
                    if self.apply:
                        # Land name/prompt first so a refused wake never drops the prompt update.
                        call("PATCH", self.p(f"/reasons/{rid}/loops/{lid}"), core)
                        self.set_wake(label, f"/reasons/{rid}/loops/{lid}", wake, schedule_only)
                        if extra:
                            call("PATCH", self.p(f"/reasons/{rid}/loops/{lid}"), extra)
                else:
                    self.note("loops", "create", label, when)
                    if self.apply:
                        if not rid:
                            raise RuntimeError("its Reason was not created")
                        try:
                            call("POST", self.p(f"/reasons/{rid}/loops"), {**core, "wake_on": wake, **extra})
                        except ApiError as e:
                            # A Loop needs a wake to exist; if the full wake is refused, create it on its
                            # schedule so the prompt lands, and report the wake that could not be added.
                            if schedule_only and schedule_only != wake:
                                call("POST", self.p(f"/reasons/{rid}/loops"), {**core, "wake_on": schedule_only, **extra})
                                self.manual.append(f"loop {label}: imported on its schedule only; its other wake was refused ({e}). Add it in Settings once its repository or channel is connected")
                            else:
                                raise
            except (ApiError, RuntimeError) as e:
                self.fail("loops", label, e)

    def set_wake(self, label: str, path: str, wake: list, schedule_only: list) -> None:
        """Replace a Loop's wakes, sent alone (the API refuses wake_on with enabled/timezone)."""
        try:
            call("PATCH", self.p(path), {"wake_on": wake})
        except ApiError as e:
            if schedule_only and schedule_only != wake:
                try:
                    call("PATCH", self.p(path), {"wake_on": schedule_only})
                    self.manual.append(f"loop {label}: kept its schedule; its other wake was refused ({e}). Add it in Settings once its repository or channel is connected")
                except ApiError as e2:
                    self.fail("loops", f"{label} wake", e2)
            else:
                self.fail("loops", f"{label} wake", e)

    def send_handoff(self, r: dict, rid: str | None) -> None:
        sent = self.st.setdefault("handoff_sent", {})
        if sent.get(r["key"]):
            self.note("handoff", "skip", r["name"], "already sent")
            return
        msg = (
            f"You are taking over from my agent \"{r['name']}\" on {self.b['source']}, which I am moving to Reason. "
            "Your instructions, memory, skills, secrets and Loops were imported already. Here is what it was doing; "
            "pick it up from here, check anything that depended on the old platform, and tell me what you need from me.\n\n"
            + r["handoff"]
        )
        self.note("handoff", "send", r["name"])
        if not self.apply:
            return
        try:
            if not rid:
                raise RuntimeError("its Reason was not created")
            res = call("POST", self.p(f"/reasons/{rid}/messages"), {"message": msg, "allow_model_rollover": True, "client_message_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"migrate:{rid}:{r['key']}:{len(msg)}"))})
            sent[r["key"]] = res.get("session_id") or res.get("session", {}).get("id") or True
            save_state(self.root, self.st)
        except (ApiError, RuntimeError) as e:
            self.fail("handoff", r["name"], e)

    # memory
    def memory(self) -> None:
        mem = self.b["_memory"]
        if not (mem["notes"] or mem["index"]) or not self.want("memory"):
            return
        for rel in mem["too_big"]:
            self.manual.append(f"memory {rel}: not migrated (binary or a section over 32 KiB)")
        scope = "team"  # memory belongs to the workspace; there is no personal memory
        self.st["memory_scope"] = scope
        targets = self.st.setdefault("memory_paths", {})
        # A bundle MEMORY.md is only an index; Reason builds and maintains its own, so it isn't imported.
        if mem["index"]:
            self.manual.append(f"memory MEMORY.md: not imported; Reason maintains the index itself as notes are written")
        notes: dict[str, str] = {}
        for rel, text in mem["notes"].items():
            t = memory_target(rel, scope, self.st.get("reasons", {}), planning=not self.apply)
            if t is None:
                self.manual.append(f"memory {rel}: its agent was not imported as a Reason, so it has no Reason folder")
                continue
            targets[rel] = t
            notes[t] = text
        for path, text in notes.items():
            if "<new " in path:
                self.note("memory", "create", f"{scope}:{path}")
                continue
            try:
                cur = self.mem_read(scope, path)
                if cur and cur.get("content", "").strip() == text.strip():
                    self.note("memory", "skip", path, "unchanged")
                    continue
                self.note("memory", "update" if cur else "create", f"{scope}:{path}")
                if self.apply:
                    # Reason auto-lists the note in its folder's and the workspace's index; don't write indexes.
                    call("PUT", self.p("/memory/file"), {"scope": scope, "path": path, "content": text, "expected_revision_id": cur.get("revision_id") if cur else None})
            except ApiError as e:
                self.fail("memory", path, e)
        if self.apply:
            save_state(self.root, self.st)

    def mem_read(self, scope: str, path: str) -> dict | None:
        try:
            res = call("GET", self.p("/memory/file"), query={"scope": scope, "path": path})
            return res.get("file", res)
        except ApiError as e:
            if e.status == 404:
                return None
            raise

    # skills
    def skills(self) -> None:
        sks = self.b["_skills"]
        if not sks or not self.want("skills"):
            return
        existing = {s["name"].strip().lower(): s for s in items(self.p("/skills"))}
        for s in sks:
            for f in s["skipped_files"]:
                self.manual.append(f"skill {s['name']}: file {f} not migrated (binary or over the inline limit)")
            body = {"name": s["name"], "description": s["description"], "instructions": s["instructions"], "is_enabled": True}
            cur = existing.get(s["name"].strip().lower())
            # Adopt a same-named skill this principal authored earlier (a re-run, or an import begun on an
            # older version that did not record skill ids), so the import updates its own work in place.
            if cur and cur["skill_id"] not in self.owned("skills") and not self.overwrite:
                if cur.get("provenance") in (None, "authored", "api") and cur.get("created_by") == self.principal:
                    self.created("skills", cur["skill_id"])
            try:
                if cur:
                    if cur.get("instructions", "").strip() == s["instructions"].strip():
                        self.note("skills", "skip", s["name"], "unchanged")
                        continue
                    if not self.mine("skills", cur["skill_id"]):
                        self.foreign("skills", s["name"])
                        continue
                    self.note("skills", "update", s["name"])
                    if self.apply:
                        call("PUT", self.p(f"/skills/{cur['skill_id']}"), body)
                else:
                    self.note("skills", "create", s["name"])
                    if self.apply:
                        # Skills belong to the workspace; there are no personal skills.
                        res = call("POST", self.p("/skills"), {**body, "installation_scope": "organization"})
                        self.created("skills", res.get("skill_id"))
            except ApiError as e:
                self.fail("skills", s["name"], e)

    # secrets
    def secrets(self) -> None:
        # MCP_OAUTH_* values exist only to feed oauth-import (mcp step); never store them as workspace secrets.
        sec = {k: v for k, v in self.b["_secrets"].items() if not k.startswith("MCP_OAUTH_")}
        if not sec or not self.want("secrets"):
            return
        # Secrets belong to the workspace; there are no personal secrets. Every member's Sessions can use them.
        try:
            have = {x.get("name") for x in items(self.p("/secrets"), {"scope": "organization"})}
        except ApiError:
            have = set()
        clash = [n for n in sec if n in have and n not in self.owned("secrets")]
        for name, value in sec.items():
            if name in clash and not self.overwrite:
                self.foreign("secrets", name)
                continue
            self.note("secrets", "update" if name in have else "create", name)
            if not self.apply:
                continue
            try:
                call("POST", self.p("/secrets"), {"name": name, "value": value, "scope": "organization", "note": f"Imported from {self.b['source']} by migrate-to-reason"})
                self.created("secrets", name)
            except ApiError as e:
                self.fail("secrets", name, e)

    # mcp
    def mcp(self) -> None:
        servers = self.b.get("mcp_servers", [])
        for c in self.b.get("connectors", []):
            self.manual.append(f"connector {c.get('name')}: sign in once in Reason > Settings > Connections" + (f" ({c['note']})" if c.get("note") else ""))
        if not servers or not self.want("mcp"):
            return
        existing = {s["name"].strip().lower(): s for s in items(self.p("/mcp-servers"))}
        sec = self.b["_secrets"]
        for s in servers:
            oauth = s.get("oauth")
            body = {k: s[k] for k in ("name", "transport", "url", "command", "args", "env", "headers", "auth_type", "description", "icon", "enabled") if s.get(k) not in (None, "", [], {})}
            secrets, missing = {}, []
            for field, mapping in (("headers", s.get("secret_headers") or {}), ("env", s.get("secret_env") or {})):
                for target, secret_name in mapping.items():
                    if secret_name in sec:
                        secrets[target] = sec[secret_name]
                    else:
                        missing.append(secret_name)
            if secrets:
                body["secrets"] = secrets
                if body.get("auth_type") in (None, "none") and s.get("secret_headers"):
                    body["auth_type"] = "bearer" if list(s["secret_headers"]) == ["Authorization"] else body.get("auth_type", "none")
            if oauth:
                # The token comes in through oauth-import; the server just carries a placeholder the
                # import fills (apps/server/src/org-mcp-servers MCP_OAUTH_ACCESS_TOKEN).
                body["auth_type"] = "oauth"
                body["transport"] = body.get("transport", "http")
                body.setdefault("headers", {})["Authorization"] = "Bearer $MCP_OAUTH_ACCESS_TOKEN"
            for m in missing:
                self.manual.append(f"mcp {s['name']}: secret {m} not in secrets.env; add it on the server in Settings > MCP")
            cur = existing.get(s["name"].strip().lower())
            if cur and not self.mine("mcp", cur["id"]):
                self.foreign("mcp", s["name"])
                continue
            try:
                if cur:
                    sid = cur["id"]
                    self.note("mcp", "update", s["name"], s.get("transport", ""))
                    if self.apply:
                        call("PATCH", self.p(f"/mcp-servers/{sid}"), {k: v for k, v in body.items() if k != "transport"})
                else:
                    sid = None
                    self.note("mcp", "create", s["name"], s.get("transport", ""))
                    if self.apply:
                        res = call("POST", self.p("/mcp-servers"), body)
                        sid = res.get("id")
                        self.created("mcp", sid)
                if oauth:
                    self.import_oauth(s["name"], sid, oauth, sec)
                elif s.get("auth_type") == "oauth":
                    self.manual.append(f"mcp {s['name']}: OAuth server; sign in once in Reason > Settings > Connections")
            except ApiError as e:
                self.fail("mcp", s["name"], e)

    def import_oauth(self, name: str, sid: str | None, oauth: dict, sec: dict) -> None:
        """Hand Reason a locally held OAuth token for this server (POST .../oauth-import)."""
        access = sec.get(oauth.get("access_token_secret", ""))
        if not access:
            self.manual.append(f"mcp {name}: its access token was not in secrets.env; sign in once in Reason > Settings > Connections")
            return
        self.note("mcp", "connect", name, oauth.get("mode", "temporary"))
        if not self.apply:
            return
        if not sid:
            self.fail("mcp", f"{name} oauth", RuntimeError("its server was not created"))
            return
        mode = "transfer" if oauth.get("mode") == "transfer" else "temporary"
        imp = {"mode": mode, "access_token": access, "expires_at": oauth.get("expires_at"),
               "scopes": oauth.get("scopes", ""), "source_application": oauth.get("source_application", self.b["source"]),
               "device_label": oauth.get("device_label", f"migrate-to-reason ({self.b['source']})"), "replace_existing": True}
        if mode == "transfer":
            refresh = sec.get(oauth.get("refresh_token_secret", ""))
            cl = oauth.get("client") or {}
            if not refresh or not cl.get("client_id"):
                self.manual.append(f"mcp {name}: transfer needs a refresh token and client id; imported the access token only")
                imp["mode"] = "temporary"
            else:
                imp["refresh_token"] = refresh
                imp["acknowledgement"] = "reason_owns_refresh_token"
                method = cl.get("token_endpoint_auth_method") or ("none" if not cl.get("client_secret_secret") else "client_secret_basic")
                client = {"client_id": cl["client_id"], "token_endpoint_auth_method": method}
                if method != "none":
                    cs = sec.get(cl.get("client_secret_secret", ""))
                    if cs:
                        client["client_secret"] = cs
                    else:
                        self.manual.append(f"mcp {name}: client secret missing; signing in once in Reason may be needed")
                imp["client"] = client
        try:
            res = call("POST", self.p(f"/mcp-servers/{sid}/oauth-import"), imp)
            if not res.get("connected"):
                self.manual.append(f"mcp {name}: token imported but not yet connected ({res})")
        except ApiError as e:
            if e.status == 422:
                self.manual.append(f"mcp {name}: its saved token was rejected (likely expired); sign in once in Reason > Settings > Connections ({e.code})")
            else:
                self.fail("mcp", f"{name} oauth", e)

    def run(self) -> int:
        who = self.self.get("organization_name") or self.self.get("organization_slug") or self.org
        print(f"{'APPLY' if self.apply else 'PLAN (dry run, nothing written)'} into workspace {who} from {self.b['source']}")
        steps = [("reasons", self.reasons), ("memory", self.memory), ("skills", self.skills), ("secrets", self.secrets), ("mcp", self.mcp)]
        for kind, fn in steps:
            if self.only and kind not in self.only and not (kind == "reasons" and self.only & {"loops", "handoff"}):
                continue
            try:
                fn()
            except ApiError as e:
                self.fail(kind, "(list)", e)
        print("\nSummary")
        for k, c in self.counts.items():
            print(f"  {k:8} " + ", ".join(f"{a}: {n}" for a, n in sorted(c.items())))
        failed = sum(c.get("failed", 0) for c in self.counts.values())
        print(f"  failed: {failed}")
        if self.manual:
            print("\nNeeds a person or could not move")
            for m in dict.fromkeys(self.manual):
                print("  - " + m)
        return 1 if failed else 0


def verify(root: Path) -> int:
    b = load_bundle(root)
    st = load_state(root)
    me = call("GET", "/v3/self")
    org = me["org_id"]
    p = lambda x: f"/v3/organizations/{org}{x}"
    bad = 0
    reasons = {r["name"].strip().lower(): r for r in items(p("/reasons"))}
    for r in b.get("reasons", []):
        got = reasons.get(r["name"].strip().lower())
        if not got:
            print(f"  MISSING reason {r['name']}"); bad += 1; continue
        if r.get("instructions") and got.get("instructions", "").strip() != r["instructions"][:16000].strip():
            print(f"  DIFF    reason {r['name']} instructions"); bad += 1
        else:
            print(f"  ok      reason {r['name']}")
        if r.get("loops"):
            loops = {l["name"].strip().lower() for l in items(p(f"/reasons/{got['id']}/loops")) if l.get("name")}
            for l in r["loops"]:
                n = (l.get("name") or l["prompt"][:60]).strip().lower()
                print(f"  {'ok     ' if n in loops else 'MISSING'} loop   {r['name']} / {n}"); bad += n not in loops
    scope = st.get("memory_scope", "team")
    for rel, text in b["_memory"]["notes"].items():
        path = st.get("memory_paths", {}).get(rel) or memory_target(rel, scope, st.get("reasons", {}))
        if not path:
            print(f"  MISSING memory {rel} (its Reason was not imported)"); bad += 1; continue
        try:
            res = call("GET", p("/memory/file"), query={"scope": scope, "path": path})
            cur = res.get("file", res)
            ok = cur.get("content", "").strip() == text.strip()
        except ApiError:
            ok = False
        print(f"  {'ok     ' if ok else 'MISSING'} memory {scope}:{path}"); bad += not ok
    if b["_skills"]:
        skills = {s["name"].strip().lower(): s for s in items(p("/skills"))}
        for s in b["_skills"]:
            got = skills.get(s["name"].strip().lower())
            ok = bool(got) and got.get("instructions", "").strip() == s["instructions"].strip()
            if got and not ok and got["skill_id"] not in st.get("created", {}).get("skills", []):
                print(f"  kept    skill  {s['name']}  (an existing skill with this name was left as it is)"); continue
            print(f"  {'ok     ' if ok else 'MISSING'} skill  {s['name']}"); bad += not ok
    if b["_secrets"]:
        sc = "organization"
        try:
            names = {x.get("name") for x in items(p("/secrets"), {"scope": sc})}
            for n in b["_secrets"]:
                print(f"  {'ok     ' if n in names else 'MISSING'} secret {sc}:{n}"); bad += n not in names
        except ApiError as e:
            print(f"  ?       secrets could not be listed ({e})")
    if b.get("mcp_servers"):
        servers = {s["name"].strip().lower(): s for s in items(p("/mcp-servers"))}
        for s in b["mcp_servers"]:
            got = servers.get(s["name"].strip().lower())
            state = (got or {}).get("connection_state")
            print(f"  {'ok     ' if got else 'MISSING'} mcp    {s['name']}" + (f"  (connection: {state})" if state else "")); bad += not got
    print(f"\nverify: {'pass' if not bad else f'{bad} mismatch(es)'}")
    return 1 if bad else 0


def undo(root: Path, apply: bool) -> int:
    """Delete what this bundle's import created, as recorded in its state file."""
    st = load_state(root)
    b = load_bundle(root)
    org = call("GET", "/v3/self")["org_id"]
    p = lambda x: f"/v3/organizations/{org}{x}"
    created = st.get("created", {})
    plan = []
    for path in st.get("memory_paths", {}).values():
        plan.append(("memory", path, "DELETE", p("/memory/file"), {"scope": "team", "path": path}))
    for sid in created.get("skills", []):
        plan.append(("skill", sid, "DELETE", p(f"/skills/{sid}"), None))
    for name in created.get("secrets", []):
        plan.append(("secret", name, "DELETE", p(f"/secrets/{name}"), {"scope": "organization"}))
    for mid in created.get("mcp", []):
        plan.append(("mcp", mid, "DELETE", p(f"/mcp-servers/{mid}"), None))
    # Deleting a Reason leaves its memory folder behind, so clean the folder's auto-maintained
    # memory.md index too. (Deleting the notes above auto-updates the workspace MEMORY.md.)
    folders = {path.rsplit("/", 1)[0] for path in st.get("memory_paths", {}).values() if path.startswith("reasons/")}
    for rid in created.get("reasons", []):
        folders.add("reasons/" + re.sub(r"[^a-z0-9]", "", rid.lower())[:12])
    for folder in sorted(folders):
        plan.append(("memory", f"{folder}/memory.md", "DELETE", p("/memory/file"), {"scope": "team", "path": f"{folder}/memory.md"}))
    for rid in created.get("reasons", []):
        try:
            for l in items(p(f"/reasons/{rid}/loops")):
                plan.append(("loop", l.get("name") or l["id"], "DELETE", p(f"/reasons/{rid}/loops/{l['id']}"), None))
        except ApiError:
            pass
        plan.append(("reason", rid, "DELETE", p(f"/reasons/{rid}"), None))
    bad = 0
    for kind, label, method, path, q in plan:
        if not apply:
            print(f"  would delete {kind:7} {label}"); continue
        try:
            call(method, path, query=q)
            print(f"  deleted      {kind:7} {label}")
        except ApiError as e:
            if e.status == 404:
                print(f"  gone         {kind:7} {label}")
            else:
                print(f"  FAILED       {kind:7} {label}  ({e})"); bad += 1
    if apply and not bad:
        (root / ".reason-import-state.json").unlink(missing_ok=True)
    print(f"\nundo: {'done' if apply and not bad else ('plan only; add --apply' if not apply else f'{bad} failed')}")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__); return 0
    cmd = argv[0]
    if cmd == "whoami":
        me = call("GET", "/v3/self")
        scopes = set(me.get("scopes", []))
        print(f"workspace: {me.get('organization_name')} ({me.get('organization_slug')}, {me.get('org_id')})")
        print(f"principal: {me.get('principal_type')} {me.get('service_user_name') or me.get('user_name') or ''}")
        print("scopes:    " + " ".join(sorted(scopes)))
        for k, need in NEEDS.items():
            miss = [s for s in need if s not in scopes]
            print(f"  {k:8} {'ready' if not miss else 'skipped, missing ' + ' '.join(miss)}")
        return 0
    if len(argv) < 2:
        print(__doc__); return 2
    root = Path(argv[1]).resolve()
    if cmd == "verify":
        return verify(root)
    if cmd == "undo":
        return undo(root, "--apply" in argv)
    only = set()
    if "--only" in argv:
        only = set(argv[argv.index("--only") + 1].split(","))
        bad = only - set(KINDS)
        if bad:
            sys.exit(f"unknown kind(s): {', '.join(bad)}; use {', '.join(KINDS)}")
    if cmd in ("plan", "apply"):
        return Run(root, cmd == "apply", "--handoff" in argv, only, "--overwrite" in argv).run()
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
