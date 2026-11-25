import test from "node:test";
import assert from "node:assert/strict";
import w, { handleUpdate, makeTg, WELCOME, PLACEHOLDER, webhookSecret } from "./worker.js";

class KV {
  constructor() { this.m = new Map(); }
  async get(k) { return this.m.has(k) ? this.m.get(k) : null; }
  async put(k, v) { this.m.set(k, v); }
  async list({ prefix }) { return { keys: [...this.m.keys()].filter((k) => k.startsWith(prefix)).map((name) => ({ name })) }; }
}
function rig(extra = {}) {
  const sent = [];
  const env = { ORDERS: new KV(), TELEGRAM_BOT_TOKEN: "123456:ABCDEFGHIJ", ADMIN_CHAT_ID: "42", PIPELINE_REPO: "o/r", PIPELINE_TOKEN: "t", UPLOAD_PUT_TEMPLATE: "https://store.example/put/{key}", UPLOAD_GET_TEMPLATE: "https://store.example/get/{key}", ...extra };
  const calls = [];
  const fetchFn = async (url, init = {}) => {
    calls.push([url, init]);
    if (url.includes("api.telegram.org/bot")) {
      const method = url.split("/").pop();
      const body = JSON.parse(init.body);
      sent.push({ method, ...body });
      return Response.json(method === "getFile" ? { ok: true, result: { file_path: "docs/f.bin" } } : { ok: true });
    }
    if (url.includes("api.telegram.org/file/")) return new Response(new Uint8Array([1, 2, 3]));
    return url.includes("github") ? new Response(null, { status: 204 }) : new Response("");
  };
  return { env, sent, calls, fetchFn, tg: makeTg(env, fetchFn) };
}
const msg = (chat, text, extra = {}) => ({ message: { chat: { id: chat }, text, ...extra } });
const press = (chat, data) => ({ callback_query: { id: "q", data, message: { chat: { id: chat }, message_id: 1 } } });

test("/start replies with the plans and the payment question", async () => {
  const r = rig();
  await handleUpdate(msg(7, "/start"), r.env, r.tg, r.fetchFn);
  const t = r.sent[0].text;
  for (const s of ["$100", "$200", "$400", "How do you want to pay?", "1. Card", "2. PayPal", "3. USDT"]) assert.ok(t.includes(s), s);
  assert.equal(t, WELCOME);
});

test("/order asks the plan, then the method, then answers even with no payment link configured", async () => {
  const r = rig();
  await handleUpdate(msg(7, "/order"), r.env, r.tg, r.fetchFn);
  assert.equal(r.sent[0].text, "Which plan?");
  assert.deepEqual(r.sent[0].reply_markup.inline_keyboard.flat().map((b) => b.callback_data), ["plan:basic_7B", "plan:standard_13B", "plan:premium_70B"]);
  await handleUpdate(press(7, "plan:basic_7B"), r.env, r.tg, r.fetchFn);
  assert.equal(r.sent.at(-1).text, "How do you want to pay?");
  await handleUpdate(press(7, "pay:basic_7B:card"), r.env, r.tg, r.fetchFn);
  const last = r.sent.at(-1); // the admin note goes first, then the client's reply
  assert.ok(last.text.includes(PLACEHOLDER) && !last.text.includes("not configured"));
  assert.ok(r.sent.at(-2).text.includes("no payment link configured") && r.sent.at(-2).chat_id === "42");
  const r2 = rig({ STRIPE_LINK: "https://pay.example/s" });
  await handleUpdate(press(7, "pay:basic_7B:card"), r2.env, r2.tg, r2.fetchFn);
  assert.ok(r2.sent.at(-1).text.includes("https://pay.example/s"));
});

test("nothing starts before the operator confirms; then data starts the pipeline once and a failed start can be retried", async () => {
  const r = rig();
  await handleUpdate(press(7, "pay:basic_7B:usdt"), r.env, r.tg, r.fetchFn);
  const id = [...r.env.ORDERS.m.keys()].find((k) => k.startsWith("o:")).slice(2);
  const doc = { document: { file_id: "f", file_size: 100 } };
  await handleUpdate(msg(7, "", doc), r.env, r.tg, r.fetchFn);
  assert.ok(r.sent.at(-1).text.includes("No paid order"));
  await handleUpdate(msg(7, `/pay ${id}`), r.env, r.tg, r.fetchFn);
  await handleUpdate(msg(7, "txid-123456"), r.env, r.tg, r.fetchFn);
  assert.ok(r.sent.some((s) => s.chat_id === "42" && s.text.includes(`/confirm ${id}`)));
  await handleUpdate(msg(7, "", doc), r.env, r.tg, r.fetchFn);
  assert.ok(r.sent.at(-1).text.includes("No paid order"), "proof is not payment");
  await handleUpdate(msg(7, `/confirm ${id}`), r.env, r.tg, r.fetchFn); // not the admin
  await handleUpdate(msg(42, `/confirm ${id}`), r.env, r.tg, r.fetchFn);
  assert.equal(JSON.parse(r.env.ORDERS.m.get("o:" + id)).state, "paid");
  await handleUpdate(msg(7, "", { document: { file_id: "f", file_size: 0 } }), r.env, r.tg, r.fetchFn);
  assert.ok(r.sent.at(-1).text.includes("Could not start"));
  assert.equal(JSON.parse(r.env.ORDERS.m.get("o:" + id)).state, "paid");
  await handleUpdate(msg(7, "", doc), r.env, r.tg, r.fetchFn);
  assert.ok(r.sent.at(-1).text.includes("is running"));
  const dispatch = r.calls.find(([u]) => u.includes("github.com"));
  const body = JSON.parse(dispatch[1].body);
  assert.deepEqual([body.ref, body.inputs.plan, body.inputs.order_id, body.inputs.telegram_chat_id, body.inputs.dry_run], ["Main", "basic_7B", id, "7", "1"]);
  assert.match(body.inputs.data_url, /^https:\/\/store\.example\/get\/o[0-9a-f]{12}-[0-9a-f]{16}$/);
  assert.equal(JSON.parse(r.env.ORDERS.m.get("o:" + id)).state, "training");
});

test("the webhook only accepts Telegram's secret; /setup needs the token's tail and sets the webhook", async () => {
  const r = rig();
  const ctx = { waitUntil: (p) => p };
  let res = await w.fetch(new Request("https://w.example/hook", { method: "POST", body: "{}" }), r.env, ctx);
  assert.equal(res.status, 403);
  res = await w.fetch(new Request("https://w.example/setup?t=nope"), r.env, ctx);
  assert.equal(res.status, 403);
  const orig = globalThis.fetch;
  const seen = [];
  globalThis.fetch = async (u, i) => { seen.push([u, i && JSON.parse(i.body)]); return Response.json({ ok: true }); };
  try {
    res = await w.fetch(new Request("https://w.example/setup?t=" + r.env.TELEGRAM_BOT_TOKEN.slice(-10)), r.env, ctx);
    assert.equal((await res.json()).ok, true);
    const set = seen.find(([u]) => u.endsWith("/setWebhook"))[1];
    assert.equal(set.url, "https://w.example/hook");
    assert.equal(set.secret_token, await webhookSecret(r.env.TELEGRAM_BOT_TOKEN));
    assert.ok(seen.some(([u, b]) => u.endsWith("/sendMessage") && b.text === "Bot is online"));
    res = await w.fetch(new Request("https://w.example/hook", { method: "POST", headers: { "x-telegram-bot-api-secret-token": set.secret_token }, body: JSON.stringify(msg(7, "/start")) }), r.env, ctx);
    assert.equal(res.status, 200);
  } finally { globalThis.fetch = orig; }
});
