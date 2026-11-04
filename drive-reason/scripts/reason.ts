#!/usr/bin/env bun
// Reason HTTP API helper for agents. Reads the key from the environment,
// resolves the workspace from GET /v3/self, checks the route's scope there and
// only then touches workspace data. No automatic retries, no token arguments,
// no URL overrides.
import { readFile, writeFile } from "node:fs/promises";

const ORIGIN = "https://api.reasonmachines.com";

// Real work uses REASON_API_KEY. Every test, probe or verification of Reason
// itself uses --tests, which reads a separate key for a separate test workspace.
// Each may be pinned to one workspace (id or slug), checked against /v3/self.
export const PROFILES = {
  work: { key: "REASON_API_KEY", pin: "REASON_WORKSPACE_ID", label: "work" },
  tests: { key: "REASON_TESTS_API_KEY", pin: "REASON_TESTS_WORKSPACE_ID", label: "test" },
} as const;
export type Profile = keyof typeof PROFILES;
type ObjectValue = Record<string, unknown>;
type Transport = (url: string, init: RequestInit) => Promise<Response>;
type Options = { id?: string; id2?: string; body?: ObjectValue; query?: ObjectValue; secretOut?: string };

type Route = {
  path: string; scope: string; method?: "POST" | "PUT" | "PATCH" | "DELETE";
  ids?: 1 | 2; body?: readonly string[]; required?: readonly string[];
  // A write whose result is a Session, read back before reporting success.
  session?: "create" | "continue" | "reason";
};
const SESSION_CREATE = ["prompt", "idempotency_key", "repo", "provider", "reason_id", "default_repositories", "target", "model", "reasoning_effort", "mode", "workspace_ownership", "hands_mode", "completion_mode", "tags", "branch", "pr_number", "ref", "base_branch", "execution_budget", "personal_context", "pull_request_policy", "spend_limit_rcu", "spend_limit_credits", "allow_model_rollover"] as const;
const MESSAGE = ["message", "client_message_id", "model", "reasoning_effort", "delivery", "allow_model_rollover"] as const;
const LOOP = ["prompt", "name", "enabled", "wake_on", "until", "max_runs", "execution_mode", "session_id", "device_id", "notification_policy", "timezone", "model"] as const;

// A Reason is the agent a person keeps: message it, give it Loops, read what it
// remembers. Sessions are its individual runs.
export const routes: Record<string, Route> = {
  reasons: { path: "/reasons", scope: "sessions:read" },
  reason: { path: "/reasons/:id", scope: "sessions:read", ids: 1 },
  "reason-create": { path: "/reasons", scope: "run", method: "POST", body: ["name", "instructions", "appearance"], required: ["name"] },
  "reason-update": { path: "/reasons/:id", scope: "run", method: "PATCH", ids: 1,
    body: ["name", "instructions", "default_model", "default_reasoning_effort", "default_repositories", "visibility"] },
  "reason-send": { path: "/reasons/:id/messages", scope: "run", method: "POST", ids: 1, body: MESSAGE, required: ["message", "client_message_id"], session: "reason" },
  "reason-messages": { path: "/reasons/:id/messages", scope: "sessions:read", ids: 1 },
  loops: { path: "/reasons/:id/loops", scope: "automations:read", ids: 1 },
  loop: { path: "/reasons/:id/loops/:id2", scope: "automations:read", ids: 2 },
  "loop-create": { path: "/reasons/:id/loops", scope: "automations:write", method: "POST", ids: 1, body: LOOP, required: ["prompt"] },
  "loop-update": { path: "/reasons/:id/loops/:id2", scope: "automations:write", method: "PATCH", ids: 2, body: [...LOOP, "schedule", "webhook"] },
  "loop-delete": { path: "/reasons/:id/loops/:id2", scope: "automations:write", method: "DELETE", ids: 2 },
  "loop-run": { path: "/reasons/:id/loops/:id2/run", scope: "automations:write", method: "POST", ids: 2 },
  "memory-list": { path: "/memory/files", scope: "memory:read" },
  "memory-read": { path: "/memory/file", scope: "memory:read" },
  "memory-search": { path: "/memory/search", scope: "memory:read" },
  "memory-write": { path: "/memory/file", scope: "memory:write", method: "PUT", body: ["scope", "path", "content", "expected_revision_id"], required: ["scope", "path", "content"] },
  "memory-delete": { path: "/memory/file", scope: "memory:write", method: "DELETE" },
  repositories: { path: "/repositories", scope: "repos:read" },
  models: { path: "/agent-auth/models", scope: "agent_auth:read" },
  list: { path: "/sessions", scope: "sessions:read" },
  get: { path: "/sessions/:id", scope: "sessions:read", ids: 1 },
  messages: { path: "/sessions/:id/messages", scope: "sessions:read", ids: 1 },
  conversation: { path: "/sessions/:id/conversation", scope: "sessions:read", ids: 1 },
  events: { path: "/sessions/:id/events", scope: "sessions:read", ids: 1 },
  trajectory: { path: "/sessions/:id/trajectory", scope: "sessions:read", ids: 1 },
  checkins: { path: "/sessions/:id/follow-ups", scope: "sessions:read", ids: 1 },
  "checkin-create": { path: "/sessions/:id/follow-ups", scope: "run", method: "POST", ids: 1, body: ["id", "at", "note"], required: ["id", "at", "note"] },
  "checkin-cancel": { path: "/sessions/:id/follow-ups/:id2", scope: "run", method: "DELETE", ids: 2 },
  create: { path: "/sessions", scope: "run", method: "POST", body: SESSION_CREATE, required: ["prompt", "idempotency_key"], session: "create" },
  send: { path: "/sessions/:id/messages", scope: "run", method: "POST", ids: 1, body: MESSAGE, required: ["message", "client_message_id"], session: "continue" },
  cancel: { path: "/sessions/:id/cancel", scope: "run", method: "POST", ids: 1, session: "continue" },
  archive: { path: "/sessions/:id/archive", scope: "run", method: "POST", ids: 1, session: "continue" },
};

function object(value: unknown): ObjectValue {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw Error("Expected a JSON object");
  return value as ObjectValue;
}
function id(value: unknown): string {
  if (typeof value !== "string" || !/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,159}$/.test(value))
    throw Error("Expected a Session, Reason, Loop or check-in ID, not a URL or path");
  return value;
}
function textField(body: ObjectValue, field: string, bytes?: number): string {
  const value = body[field];
  if (typeof value !== "string" || !value.trim()) throw Error(`${field} must be a nonempty string`);
  if (bytes && Buffer.byteLength(value) > bytes) throw Error(`${field} exceeds its UTF-8 byte limit`);
  return value;
}

export class Failure extends Error {
  constructor(message: string, readonly details: ObjectValue = {}) { super(message); }
}

export class Reason {
  private readonly profile: (typeof PROFILES)[Profile];
  constructor(
    private readonly token: string,
    private readonly transport: Transport = fetch,
    profile: Profile = "work",
    private readonly pin?: string,
    // With --tests: the work key, used only to prove the test workspace differs.
    private readonly workToken?: string,
  ) {
    this.profile = PROFILES[profile];
    if (!token.trim()) throw Error(`${this.profile.key} is not set; export a Reason API key for the ${this.profile.label} workspace (see SKILL.md)`);
  }

  private async request(path: string, body?: ObjectValue, query: ObjectValue = {}, method?: string, token = this.token) {
    const url = new URL(path, ORIGIN);
    if (url.origin !== ORIGIN || !url.pathname.startsWith("/v3/")) throw Error("API origin/path refused");
    for (const [key, value] of Object.entries(query)) {
      if (!["string", "number", "boolean"].includes(typeof value)) throw Error("Query values must be scalar");
      url.searchParams.set(key, String(value));
    }
    let response: Response;
    try {
      response = await this.transport(url.toString(), {
        method: method ?? (body === undefined ? "GET" : "POST"),
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", "X-Reason-Agent": "drive-reason" },
        body: body === undefined ? undefined : JSON.stringify(body),
        redirect: "error", signal: AbortSignal.timeout(30_000),
      });
    } catch {
      throw new Failure("Transport failed or redirect refused; reconcile state before retrying", { mutation_state: method || body ? "unknown" : "not_sent" });
    }
    // Never echo raw response bodies, which can contain prompts or credentials.
    if (response.status === 204) return { http_status: 204, data: {} };
    let data: unknown;
    try { data = await response.json(); } catch {
      throw new Failure("API returned non-JSON; reconcile any mutation before retrying", { http_status: response.status });
    }
    if (!response.ok) {
      const error = data && typeof data === "object" && !Array.isArray(data) ? data as ObjectValue : {};
      const auth = response.status === 401 || response.status === 403;
      throw new Failure(`API HTTP ${response.status}${auth ? "; do not retry with this key, fix the key or its scopes" : ""}`, {
        http_status: response.status, retry_after: response.headers.get("Retry-After"),
        error: error.error, required_scope: error.required_scope,
      });
    }
    return { http_status: response.status, data: object(data) };
  }

  // A test key must not reach the work workspace: compare both keys' /v3/self
  // workspaces, or rely on an explicit test-workspace pin when no work key is set.
  private async ensureSeparateTestWorkspace(testOrg: string) {
    if (!this.workToken) {
      if (this.pin) return;
      throw Error("--tests needs REASON_TESTS_WORKSPACE_ID, or REASON_API_KEY to prove the test workspace is a different one; no workspace operation sent");
    }
    const work = await this.request("/v3/self", undefined, {}, undefined, this.workToken);
    if (work.data.org_id === testOrg)
      throw Error("REASON_TESTS_API_KEY belongs to the same workspace as REASON_API_KEY; tests need a separate test workspace. No workspace operation sent");
  }

  async execute(command: string, options: Options = {}) {
    const route = routes[command];
    if (command !== "self" && !route) throw Error("Unknown command; run --help");
    const ids = route?.ids ?? 0;
    if (ids >= 1) id(options.id); else if (options.id) throw Error("This command does not take an ID");
    if (ids === 2) id(options.id2); else if (options.id2) throw Error("This command takes one ID");
    if (options.body && !route?.body) throw Error("Body not allowed for this command");
    if (route?.body && !options.body) throw Error("This command needs --body FILE");
    if ((route?.method === "POST" || route?.method === "PATCH" || route?.method === "PUT" || command === "self") && options.query)
      throw Error("Query not allowed for this command");
    if (options.body && route?.body) {
      const body = options.body;
      if (Object.keys(body).some(key => !route.body!.includes(key))) throw Error("Unsupported body field; consult the live API contract and skill scope");
      for (const field of route.required ?? []) textField(body, field, field === "prompt" ? 256 * 1024 : field === "message" ? 2 * 1024 * 1024 : undefined);
      if (body.client_message_id !== undefined && String(body.client_message_id).length > 160) throw Error("client_message_id exceeds 160 characters");
      if (["branch", "pr_number", "ref"].filter(key => body[key] !== undefined).length > 1)
        throw Error("branch, pr_number and ref are mutually exclusive");
      if (body.base_branch !== undefined && body.ref === undefined) throw Error("base_branch requires ref");
      if (command === "loop-create" && (!Array.isArray(body.wake_on) || body.wake_on.length === 0))
        throw Error("loop-create needs wake_on: a schedule, webhook, github or slack wake");
      // A webhook Loop's key is returned once; it must land in a private file, never in output.
      const wakes = Array.isArray(body.wake_on) ? body.wake_on : [];
      if ((body.webhook !== undefined || wakes.some(wake => wake && typeof wake === "object" && "webhook" in wake)) && !options.secretOut)
        throw Error("A webhook Loop returns its key once: pass --secret-out FILE to keep it");
    }

    // Resolve and verify the workspace before any workspace operation.
    const self = await this.request("/v3/self");
    const org = self.data.org_id;
    if (typeof org !== "string" || !org) throw Error("Credential has no workspace; no workspace operation sent");
    const workspace = { id: org, name: self.data.organization_name ?? null, slug: self.data.organization_slug ?? null };
    if (this.pin && this.pin !== org && this.pin !== workspace.slug)
      throw Error(`Workspace mismatch: the key belongs to ${workspace.name ?? org}, not the pinned ${this.profile.pin}; no workspace operation sent`);
    if (command === "self") return self;
    if (this.profile === PROFILES.tests) await this.ensureSeparateTestWorkspace(org);
    const scopes = self.data.scopes;
    if (!Array.isArray(scopes) || !scopes.every(s => typeof s === "string")) throw Error("Invalid credential scope response");
    // Writes to an existing Session read it first: to observe it, and to see
    // whether it runs on a Device, which needs devices:use as well as run.
    const preflight = route.session === "continue" || command === "checkin-create";
    const required = [route.scope, ...(route.session || preflight ? ["sessions:read"] : [])];
    if (options.body?.target && object(options.body.target).kind !== "cloud") required.push("devices:use");
    for (const scope of required) if (!scopes.includes(scope)) throw Error(`Missing scope ${scope}; no workspace operation sent`);
    const base = `/v3/organizations/${encodeURIComponent(org)}`;
    const path = base + route.path.replace(":id2", options.id2 ?? "").replace(":id", options.id ?? "");
    if (!route.method) return { workspace, ...(await this.request(path, undefined, options.query)) };
    if (preflight) {
      const current = await this.request(`${base}/sessions/${id(options.id)}`);
      if (command === "cancel" && current.data.status !== "running")
        return { workspace, mutation_sent: false, session: current.data };
      const target = current.data.target;
      const onDevice = !!target && typeof target === "object" && (target as ObjectValue).kind !== "cloud";
      if ((command === "send" || command === "checkin-create") && onDevice && !scopes.includes("devices:use"))
        throw Error("Missing scope devices:use for a Device-backed Session; no workspace operation sent");
    }
    const query = route.method === "DELETE" ? options.query : undefined;
    const accepted = await this.request(path, options.body ?? (route.method === "POST" ? {} : undefined), query, route.method);
    if (options.secretOut) await keepWebhook(accepted.data, options.secretOut);
    else if (accepted.data.webhook && typeof accepted.data.webhook === "object") accepted.data.webhook = "[withheld: rerun with --secret-out FILE to keep the url and key]";
    if (!route.session) return { workspace, ...accepted };
    try {
      const sessionId = id(route.session === "continue" ? accepted.data.continuation_session_id ?? options.id : accepted.data.session_id);
      const observed = await this.request(`${base}/sessions/${sessionId}`);
      return { workspace, accepted, session: observed.data };
    } catch (error) {
      throw new Failure("Mutation accepted but readback failed; retrieve the accepted Session before any repeat", {
        accepted, readback_error: error instanceof Error ? error.message : "unknown",
      });
    }
  }
}

// Moves a returned webhook { url, key } into a private file and out of the printed result.
async function keepWebhook(data: ObjectValue, file: string) {
  const webhook = data.webhook;
  if (!webhook || typeof webhook !== "object") return;
  await writeFile(file, JSON.stringify(webhook), { mode: 0o600, flag: "wx" });
  data.webhook = `[url and key written to ${file}]`;
}

const HELP = `Usage: bun reason.ts COMMAND [ID] [ID2] [--query JSON] [--body FILE] [--secret-out FILE] [--tests]
Reasons:  reasons, reason ID, reason-create --body, reason-update ID --body, reason-send ID --body, reason-messages ID
Loops:    loops REASON_ID, loop REASON_ID LOOP_ID, loop-create REASON_ID --body, loop-update REASON_ID LOOP_ID --body,
          loop-run REASON_ID LOOP_ID, loop-delete REASON_ID LOOP_ID
Memory:   memory-list|memory-read|memory-search|memory-delete --query '{"scope":"personal|team",...}', memory-write --body
Sessions: list get messages conversation events trajectory create send cancel archive
Check-ins: checkins SESSION_ID, checkin-create SESSION_ID --body, checkin-cancel SESSION_ID CHECKIN_ID
Other:    self repositories models
create: prompt + idempotency_key. send / reason-send: message + client_message_id. loop-create: prompt + wake_on.
checkin-create: id (a UUID you generate) + at (ISO time) + note.
A webhook Loop's key comes back once: --secret-out FILE writes { url, key } there (mode 600) instead of printing it.
Key: REASON_API_KEY (or REASON_TESTS_API_KEY with --tests). Optional pin: REASON_WORKSPACE_ID
(or REASON_TESTS_WORKSPACE_ID), a workspace id or slug checked against /v3/self.
Every call checks /v3/self and the route's scope first. No automatic retries.`;

export async function main(args: string[], env: Record<string, string | undefined> = process.env) {
  if (args.length === 0 || (args.length === 1 && args[0] === "--help")) { console.log(HELP); return; }
  const profile: Profile = args.includes("--tests") ? "tests" : "work";
  args = args.filter(arg => arg !== "--tests");
  const token = env[PROFILES[profile].key] ?? "";
  const pin = env[PROFILES[profile].pin]?.trim() || undefined;
  const print = (value: unknown) => {
    let output = JSON.stringify(value, null, 2);
    for (const secret of [env.REASON_API_KEY, env.REASON_TESTS_API_KEY])
      if (secret && secret.length >= 8) output = output.split(secret).join("[redacted]");
    console.log(output.replace(/(?:reason_|ara_)[a-fA-F0-9]{64}/g, "[redacted]"));
  };
  try {
    // A test run must never reach the work workspace through a shared key.
    if (profile === "tests" && token && token === env.REASON_API_KEY)
      throw Error("REASON_TESTS_API_KEY equals REASON_API_KEY; tests need a key for a separate test workspace");
    const [command, ...rest] = args;
    const options: Options = {};
    for (let index = 0; index < rest.length; index++) {
      const arg = rest[index];
      if (arg === "--secret-out") {
        const value = rest[++index];
        if (!value || value.startsWith("--") || options.secretOut) throw Error("--secret-out needs one file path");
        options.secretOut = value;
      } else if (arg === "--query" || arg === "--body") {
        const value = rest[++index];
        if (!value || value.startsWith("--")) throw Error(`Missing value for ${arg}`);
        const field = arg === "--query" ? "query" : "body";
        if (options[field]) throw Error(`Duplicate ${arg}`);
        let raw: string;
        try { raw = field === "body" ? await readFile(value, "utf8") : value; }
        catch { throw Error("Could not read the request file"); }
        try { options[field] = object(JSON.parse(raw)); }
        catch { throw Error(`Invalid JSON object for ${arg}`); }
      } else if (!arg.startsWith("--") && !options.id) options.id = arg;
      else if (!arg.startsWith("--") && !options.id2) options.id2 = arg;
      else throw Error("Unexpected argument; run --help");
    }
    const workToken = profile === "tests" ? env.REASON_API_KEY?.trim() || undefined : undefined;
    print(await new Reason(token, fetch, profile, pin, workToken).execute(command, options));
  } catch (error) {
    print({ error: error instanceof Error ? error.message : "Unknown failure", ...(error instanceof Failure ? error.details : {}) });
    process.exitCode = 1;
  }
}

if (import.meta.main) await main(process.argv.slice(2));
