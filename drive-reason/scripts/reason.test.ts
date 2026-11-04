import { describe, expect, test, spyOn } from "bun:test";
import { mkdtemp, readFile, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Reason, main } from "./reason";

// Fixture workspace: any value works, the helper reads it from /v3/self.
const org = "00000000-0000-4000-8000-000000000001";
const self = {
  org_id: org, organization_name: "Example Workspace", organization_slug: "example",
  scopes: ["run", "sessions:read", "repos:read", "agent_auth:read", "automations:read", "automations:write", "memory:read", "memory:write"],
};
const session = { session_id: "ses_test", status: "running", url: "https://reasonmachines.com/agents/ses_test" };
const json = (data: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(data), { status, headers });
function fixture(responses: Array<Response | Error>, pin?: string) {
  const calls: Array<{ url: URL; init: RequestInit }> = [];
  const client = new Reason("test-only-credential", async (url, init) => {
    calls.push({ url: new URL(url), init });
    expect(new URL(url).origin).toBe("https://api.reasonmachines.com");
    expect(init.redirect).toBe("error");
    expect(init.headers).toMatchObject({ Authorization: "Bearer test-only-credential" });
    const response = responses.shift();
    if (!response) throw Error("Unexpected request");
    if (response instanceof Error) throw response;
    return response;
  }, "work", pin);
  return { client, calls };
}

describe("identity and bounded request routing", () => {
  test("rejects missing credentials and names the variable", () => {
    expect(() => new Reason("")).toThrow("REASON_API_KEY");
    expect(() => new Reason("", fetch, "tests")).toThrow("REASON_TESTS_API_KEY");
  });
  test("resolves the workspace from /v3/self, with no hard-coded workspace", async () => {
    const { client, calls } = fixture([json(self), json({ items: [] })]);
    const output = await client.execute("reasons");
    expect(calls[0].url.pathname).toBe("/v3/self");
    expect(calls[1].url.pathname).toBe(`/v3/organizations/${org}/reasons`);
    expect(output).toMatchObject({ workspace: { id: org, slug: "example" } });
  });
  test("a workspace pin that does not match stops before any workspace operation", async () => {
    const { client, calls } = fixture([json(self)], "another-workspace");
    await expect(client.execute("create", { body: { prompt: "test", idempotency_key: "one" } })).rejects.toThrow("Workspace mismatch");
    expect(calls).toHaveLength(1);
    expect(calls[0].url.pathname).toBe("/v3/self");
  });
  test("a pin matches by workspace id or slug", async () => {
    for (const pin of [org, "example"]) {
      const { client, calls } = fixture([json(self), json({ items: [] })], pin);
      await client.execute("list");
      expect(calls).toHaveLength(2);
    }
  });
  test("a key without a workspace is refused", async () => {
    const { client, calls } = fixture([json({ ...self, org_id: null })]);
    await expect(client.execute("list")).rejects.toThrow("no workspace");
    expect(calls).toHaveLength(1);
  });
  test("requires run and readback scopes before writes", async () => {
    for (const scopes of [["run"], ["sessions:read"]]) {
      const { client, calls } = fixture([json({ ...self, scopes })]);
      await expect(client.execute("cancel", { id: "ses_test" })).rejects.toThrow("Missing scope");
      expect(calls).toHaveLength(1);
    }
  });
  test("requires device scope for explicit machine target", async () => {
    const { client, calls } = fixture([json(self)]);
    await expect(client.execute("create", { body: { prompt: "test", idempotency_key: "one", target: { kind: "mac" } } })).rejects.toThrow("devices:use");
    expect(calls).toHaveLength(1);
  });
  test("refuses URL, path and administrative commands without making requests", async () => {
    const { client, calls } = fixture([]);
    for (const id of ["https://evil.example/session", "../secrets", "ses_a?query=bad"])
      await expect(client.execute("get", { id })).rejects.toThrow("not a URL or path");
    await expect(client.execute("delete", { id: "ses_test" })).rejects.toThrow("Unknown command");
    expect(calls).toHaveLength(0);
  });
  test("propagates pagination without changing the origin", async () => {
    const { client, calls } = fixture([json(self), json({ items: [], has_next_page: true, end_cursor: "next" })]);
    const output = await client.execute("list", { query: { after: "https://evil.example/a?b=c", first: 10 } });
    expect(calls[1].url.searchParams.get("after")).toBe("https://evil.example/a?b=c");
    expect(calls[1].url.pathname).toBe(`/v3/organizations/${org}/sessions`);
    expect(output).toMatchObject({ data: { end_cursor: "next", has_next_page: true } });
  });
  test("reads events including sequence zero", async () => {
    const { client, calls } = fixture([json(self), json({ session, items: [], next_seq: 12 })]);
    await client.execute("events", { id: "ses_test", query: { after_seq: -1, limit: 100 } });
    expect(calls[1].url.searchParams.get("after_seq")).toBe("-1");
  });
});

describe("mutations and reconciliation", () => {
  test("requires a stable body idempotency key", async () => {
    const { client, calls } = fixture([]);
    await expect(client.execute("create", { body: { prompt: "test" } })).rejects.toThrow("idempotency_key");
    await expect(client.execute("send", { id: "ses_test", body: { message: "test" } })).rejects.toThrow("client_message_id");
    await expect(client.execute("checkin-create", { id: "ses_test", body: { at: "2030-01-01T00:00:00Z", note: "x" } })).rejects.toThrow("id must be");
    expect(calls).toHaveLength(0);
  });
  test("creates and replays with identical caller body then reads back", async () => {
    for (const status of [201, 200]) {
      const { client, calls } = fixture([json(self), json(session, status), json(session)]);
      const body = { prompt: "test", idempotency_key: "stable-one", reason_id: "reason-1" };
      const output = await client.execute("create", { body });
      expect(JSON.parse(String(calls[1].init.body))).toEqual(body);
      expect(calls.map(c => c.init.method)).toEqual(["GET", "POST", "GET"]);
      expect(output).toMatchObject({ accepted: { http_status: status }, session });
    }
  });
  test("sends a continuation and observes returned stable identity", async () => {
    const { client, calls } = fixture([json(self), json({ ...session, status: "exit" }), json({ ok: true, continuation_session_id: "ses_stable" }), json({ ...session, session_id: "ses_stable" })]);
    const body = { message: "continue", client_message_id: "follow-one" };
    await client.execute("send", { id: "ses_test", body });
    expect(JSON.parse(String(calls[2].init.body))).toEqual(body);
    expect(calls[3].url.pathname).toEndWith("/sessions/ses_stable");
  });
  test("retains accepted receipt if readback fails", async () => {
    const { client, calls } = fixture([json(self), json(session, 201), new Error("read failed")]);
    let failure: unknown;
    try { await client.execute("create", { body: { prompt: "test", idempotency_key: "one" } }); } catch (error) { failure = error; }
    expect(failure).toMatchObject({ details: { accepted: { http_status: 201, data: session } } });
    expect(calls.filter(call => call.init.method === "POST")).toHaveLength(1);
  });
  test("cancellation acceptance remains running until observed otherwise", async () => {
    const { client } = fixture([json(self), json(session), json({ ok: true, requested: true, status: "running" }), json(session)]);
    expect(await client.execute("cancel", { id: "ses_test" })).toMatchObject({ session: { status: "running" } });
  });
  test("does not cancel a session already terminal", async () => {
    const { client, calls } = fixture([json(self), json({ ...session, status: "exit", outcome: "failure" })]);
    expect(await client.execute("cancel", { id: "ses_test" })).toMatchObject({ mutation_sent: false, session: { outcome: "failure" } });
    expect(calls).toHaveLength(2);
  });
  test("refuses conflicting repository refs and credential injection", async () => {
    const { client, calls } = fixture([]);
    const base = { prompt: "test", idempotency_key: "one" };
    for (const body of [{ ...base, branch: "x", ref: "y" }, { ...base, base_branch: "main" }, { ...base, env: { TOKEN: "x" } }])
      await expect(client.execute("create", { body })).rejects.toThrow();
    expect(calls).toHaveLength(0);
  });
  test("validates UTF-8 bytes and client message ID length", async () => {
    const { client } = fixture([]);
    await expect(client.execute("create", { body: { prompt: "🙂".repeat(65537), idempotency_key: "one" } })).rejects.toThrow("byte limit");
    await expect(client.execute("send", { id: "ses_test", body: { message: "test", client_message_id: "a".repeat(161) } })).rejects.toThrow("160");
  });
  test("schedules and cancels a Session check-in", async () => {
    const { client, calls } = fixture([json(self), json({ ok: true }, 201), json(self), json({}, 204)]);
    const body = { id: "11111111-1111-4111-8111-111111111111", at: "2030-01-01T00:00:00Z", note: "check CI" };
    await client.execute("checkin-create", { id: "ses_test", body });
    expect(calls[1].url.pathname).toBe(`/v3/organizations/${org}/sessions/ses_test/follow-ups`);
    expect(JSON.parse(String(calls[1].init.body))).toEqual(body);
    await client.execute("checkin-cancel", { id: "ses_test", id2: body.id });
    expect(calls[3].init.method).toBe("DELETE");
  });
});

describe("errors do not silently retry or leak transport details", () => {
  test("surfaces rate limit and Retry-After without retry", async () => {
    const { client, calls } = fixture([json(self), json({ error: "rate_limited" }, 429, { "Retry-After": "42" })]);
    let failure: unknown;
    try { await client.execute("list"); } catch (error) { failure = error; }
    expect(failure).toMatchObject({ details: { http_status: 429, retry_after: "42" } });
    expect(calls).toHaveLength(2);
  });
  test("401 and 403 stop at once and say not to retry", async () => {
    for (const status of [401, 403]) {
      const { client, calls } = fixture([json({ error: "unauthorized" }, status)]);
      await expect(client.execute("list")).rejects.toThrow("do not retry");
      expect(calls).toHaveLength(1);
    }
  });
  test("hides transport exception payloads", async () => {
    const { client, calls } = fixture([new Error("secret-from-transport")]);
    let failure: unknown;
    try { await client.execute("list"); } catch (error) { failure = error; }
    expect(String(failure)).not.toContain("secret-from-transport");
    expect(calls).toHaveLength(1);
  });
  test("reports an ambiguous POST without automatic repeat", async () => {
    const { client, calls } = fixture([json(self), new Error("timeout")]);
    let failure: unknown;
    try { await client.execute("create", { body: { prompt: "test", idempotency_key: "one" } }); } catch (error) { failure = error; }
    expect(failure).toMatchObject({ details: { mutation_state: "unknown" } });
    expect(calls).toHaveLength(2);
  });
});

describe("Reasons, Loops and memory", () => {
  test("messages a Reason and reads back its conversation Session", async () => {
    const { client, calls } = fixture([json(self), json({ session_id: "ses_conv", reason_id: "rsn_1", conversation_created: true }, 201), json({ ...session, session_id: "ses_conv" })]);
    const body = { message: "hi", client_message_id: "m-1" };
    const output = await client.execute("reason-send", { id: "rsn_1", body });
    expect(calls[1].url.pathname).toBe(`/v3/organizations/${org}/reasons/rsn_1/messages`);
    expect(calls[2].url.pathname).toEndWith("/sessions/ses_conv");
    expect(output).toMatchObject({ session: { session_id: "ses_conv" } });
  });
  test("addresses a Loop under its Reason with the right method and scope", async () => {
    const { client, calls } = fixture([json(self), json({}, 204)]);
    await client.execute("loop-delete", { id: "rsn_1", id2: "loop_1" });
    expect(calls[1].init.method).toBe("DELETE");
    expect(calls[1].url.pathname).toBe(`/v3/organizations/${org}/reasons/rsn_1/loops/loop_1`);
    const missing = fixture([json({ ...self, scopes: ["run", "sessions:read"] })]);
    await expect(missing.client.execute("loops", { id: "rsn_1" })).rejects.toThrow("automations:read");
  });
  test("a Loop needs a wake", async () => {
    await expect(fixture([]).client.execute("loop-create", { id: "rsn_1", body: { prompt: "x" } })).rejects.toThrow("wake_on");
  });
  test("runs a Loop now", async () => {
    const { client, calls } = fixture([json(self), json({ loop: { id: "loop_1" } }, 202)]);
    const output = await client.execute("loop-run", { id: "rsn_1", id2: "loop_1" });
    expect(calls[1].url.pathname).toEndWith("/reasons/rsn_1/loops/loop_1/run");
    expect(output).toMatchObject({ http_status: 202 });
  });
  test("keeps a webhook Loop's key out of the output", async () => {
    const body = { prompt: "watch", wake_on: [{ webhook: {} }] };
    await expect(fixture([]).client.execute("loop-create", { id: "rsn_1", body })).rejects.toThrow("--secret-out");
    const file = join(await mkdtemp(join(tmpdir(), "drive-reason-")), "webhook.json");
    const webhook = { url: "https://api.reasonmachines.com/webhooks/automations/t", key: "not-a-real-key" };
    const { client } = fixture([json(self), json({ loop: { id: "loop_1" }, webhook }, 201)]);
    const output = await client.execute("loop-create", { id: "rsn_1", body, secretOut: file });
    expect(JSON.stringify(output)).not.toContain("not-a-real-key");
    expect(JSON.parse(await readFile(file, "utf8"))).toEqual(webhook);
    expect((await stat(file)).mode & 0o777).toBe(0o600);
  });
  test("withholds a webhook key returned without --secret-out", async () => {
    const { client } = fixture([json(self), json({ loop: { id: "loop_1" }, webhook: { url: "u", key: "not-a-real-key" } }, 202)]);
    const output = await client.execute("loop-run", { id: "rsn_1", id2: "loop_1" });
    expect(JSON.stringify(output)).not.toContain("not-a-real-key");
  });
  test("passes memory scope and path as query, writes with PUT", async () => {
    const { client, calls } = fixture([json(self), json({ items: [] }), json(self), json({ updated: true })]);
    await client.execute("memory-read", { query: { scope: "team", path: "notes/a.md" } });
    expect(calls[1].url.searchParams.get("path")).toBe("notes/a.md");
    await client.execute("memory-write", { body: { scope: "personal", path: "a.md", content: "x" } });
    expect(calls[3].init.method).toBe("PUT");
    await expect(client.execute("memory-write", { body: { scope: "personal", path: "a.md", content: "x", owner: "someone" } })).rejects.toThrow("Unsupported body field");
  });
});

describe("command line", () => {
  test("a missing key fails without a request", async () => {
    const log = spyOn(console, "log").mockImplementation(() => {});
    await main(["self"], {});
    expect(String(log.mock.calls[0][0])).toContain("REASON_API_KEY is not set");
    log.mockRestore(); process.exitCode = 0;
  });
  test("--tests refuses a test key equal to the work key", async () => {
    const log = spyOn(console, "log").mockImplementation(() => {});
    await main(["self", "--tests"], { REASON_API_KEY: "same-key-123", REASON_TESTS_API_KEY: "same-key-123" });
    const printed = String(log.mock.calls[0][0]);
    expect(printed).toContain("separate test workspace");
    expect(printed).not.toContain("same-key-123");
    log.mockRestore(); process.exitCode = 0;
  });
});
