#!/usr/bin/env bun
// Blocks until a handed-off Reason Session needs a look, then prints one JSON
// summary and exits. Run it as a background command so its exit wakes the
// local agent (Claude Code: Bash with run_in_background). It calls the
// drive-reason helper, which reads REASON_API_KEY (or REASON_TESTS_API_KEY
// with --tests) from the environment; the key is never passed as an argument.
//
// Exit reasons: turn_ended (the Session stopped running: read its reply),
// stalled (no new events for --stall-min while running), heartbeat (--max-min
// passed: give a progress update and re-arm), error (the helper failed).
import { existsSync, realpathSync } from "node:fs";
import { dirname, join } from "node:path";

const args = process.argv.slice(2);
const flag = (name: string, fallback: number) => {
  const at = args.indexOf(`--${name}`);
  const value = at >= 0 ? Number(args[at + 1]) : fallback;
  if (!Number.isFinite(value)) { console.error(`--${name} needs a number`); process.exit(2); }
  return value;
};
const sessionId = args[0];
if (!sessionId || sessionId.startsWith("--")) {
  console.error("Usage: bun watch.ts SESSION_ID [--after-seq N] [--stall-min 10] [--max-min 15] [--start-grace-min 2] [--tests]");
  process.exit(2);
}
const tests = args.includes("--tests");
const stallMs = flag("stall-min", 10) * 60_000;
const maxMs = flag("max-min", 15) * 60_000;
// How long an already-ended Session may stay quiet before that counts as its
// turn having ended: raise it to wait for a scheduled check-in.
const graceMs = flag("start-grace-min", 2) * 60_000;
let afterSeq = flag("after-seq", -1);

// drive-reason is installed beside this skill (skills/<name>/...).
const helper = join(dirname(realpathSync(import.meta.path)), "../../drive-reason/scripts/reason.ts");
if (!existsSync(helper)) {
  console.error(`drive-reason helper not found at ${helper}; install drive-reason next to work-in-reason`);
  process.exit(2);
}
const ENDED = new Set(["exit", "error", "suspended", "cancelled", "canceled", "failed"]);

type Item = { seq: number; type: string; text: string | null; created_at: string };
type Page = { session: { status: string; url: string; error_class?: string | null }; items: Item[]; next_seq?: number };

async function events(query: object): Promise<Page> {
  const cmd = ["bun", helper, "events", sessionId, "--query", JSON.stringify(query), ...(tests ? ["--tests"] : [])];
  const proc = Bun.spawn(cmd, { stdout: "pipe", stderr: "pipe" });
  const [out, err, code] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text(), proc.exited]);
  if (code !== 0) {
    const message = (err || out).trim().slice(0, 500);
    // A rejected key or missing scope will not fix itself: stop at once.
    if (/HTTP 40[13]|not set|Missing scope|Workspace mismatch/.test(message)) done("error", { error: message });
    throw new Error(message);
  }
  return JSON.parse(out).data as Page;
}

const started = Date.now();
let lastActivity = Date.now();
let newEvents = 0;
let lastAssistant = "";
let status = "unknown";
let url = "";
let failures = 0;
let catchingUp = afterSeq < 0;
// A message just sent may not have started yet: the previous turn still reads
// as ended. Treat "ended" as final once this watch saw it active or saw new
// events, or after --start-grace-min.
let sawActive = false;

function done(reason: string, extra: object = {}): never {
  console.log(JSON.stringify({
    exit_reason: reason, session_id: sessionId, url, status, next_seq: afterSeq, new_events: newEvents,
    minutes_watched: Math.round((Date.now() - started) / 6000) / 10,
    minutes_since_activity: Math.round((Date.now() - lastActivity) / 6000) / 10,
    last_assistant: lastAssistant.slice(-1500), ...extra,
  }, null, 2));
  process.exit(0);
}

while (true) {
  let page: Page;
  try {
    page = await events({ after_seq: afterSeq, limit: 500, ...(catchingUp ? {} : { wait_ms: 20000 }) });
    failures = 0;
  } catch (error) {
    if (++failures >= 3) done("error", { error: String(error) });
    await Bun.sleep(5000);
    continue;
  }
  status = page.session.status;
  if (!ENDED.has(status)) sawActive = true;
  url = page.session.url;
  for (const item of page.items) {
    if (item.type === "assistant" && item.text) lastAssistant = item.text;
  }
  if (page.items.length) {
    if (!catchingUp) newEvents += page.items.length;
    lastActivity = Date.now();
  }
  if (typeof page.next_seq === "number") afterSeq = page.next_seq;
  // Reading the backlog from the start: page through it without waiting.
  if (catchingUp && page.items.length === 500) continue;
  catchingUp = false;

  if (ENDED.has(status) && (sawActive || newEvents > 0 || Date.now() - started >= graceMs)) done("turn_ended", { error_class: page.session.error_class ?? null });
  if (status === "running" && Date.now() - lastActivity >= stallMs) done("stalled");
  if (Date.now() - started >= maxMs) done("heartbeat");
  // The server long-polls only running Sessions; an ended one waiting out its
  // grace period answers at once, so pace those polls.
  if (ENDED.has(status) && page.items.length === 0) await Bun.sleep(15_000);
}
