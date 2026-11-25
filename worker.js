// Webhook version of the order bot for Cloudflare Workers: no server, no polling. Same flow as bot.py.
// Bindings: KV namespace ORDERS; secrets TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID, STRIPE_LINK, PAYPAL_LINK, USDT_WALLET,
// PIPELINE_REPO, PIPELINE_TOKEN, UPLOAD_PUT_TEMPLATE, UPLOAD_GET_TEMPLATE; optional PIPELINE_REF (default Main), DRY_RUN (default 1).

export const PLANS = { basic_7B: ["Basic 7B", 100], standard_13B: ["Standard 13B", 200], premium_70B: ["Premium 70B", 400] };
export const METHODS = { card: ["Card", "STRIPE_LINK"], paypal: ["PayPal", "PAYPAL_LINK"], usdt: ["USDT", "USDT_WALLET"] };
export const MAX_DATA_BYTES = 20 << 20; // the bot API's own download limit
export const PLACEHOLDER = "Payment link will be sent after you confirm";
export const WELCOME =
  "Welcome! Plans:\n" + Object.values(PLANS).map(([n, p]) => `- ${n} $${p}`).join("\n") +
  "\n\nHow do you want to pay?\n1. Card\n2. PayPal\n3. USDT\n\nSend /order to start.";

const NEXT = {
  awaiting_payment: ["payment_submitted", "rejected"],
  payment_submitted: ["paid", "rejected"],
  paid: ["data_received"],
  data_received: ["training"],
  training: ["done", "failed"],
};

export class OrderError extends Error {}

async function load(env, id) {
  const raw = await env.ORDERS.get("o:" + id);
  if (!raw) throw new OrderError("no such order");
  return JSON.parse(raw);
}
async function save(env, id, o) {
  await env.ORDERS.put("o:" + id, JSON.stringify(o));
  await env.ORDERS.put(`c:${o.chat}:${id}`, o.state);
}
async function move(env, id, o, to) {
  if (!(NEXT[o.state] || []).includes(to)) throw new OrderError(`cannot go from ${o.state} to ${to}`);
  o.state = to;
  await save(env, id, o);
}
async function openFor(env, chat, ...states) {
  const l = await env.ORDERS.list({ prefix: `c:${chat}:` });
  const out = [];
  for (const k of l.keys) {
    const id = k.name.split(":")[2];
    const o = JSON.parse((await env.ORDERS.get("o:" + id)) || "null");
    if (o && states.includes(o.state)) out.push(id);
  }
  return out.sort((a, b) => ("" + a).localeCompare("" + b));
}
function newId() {
  const b = new Uint8Array(6);
  crypto.getRandomValues(b);
  return "o" + [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
}
export function paymentTarget(env, method) {
  return env[METHODS[method][1]] || ""; // empty = not configured yet: the order is still taken and the operator sends the link
}

export function makeTg(env, fetchFn = fetch) {
  const call = async (method, body) => {
    const r = await fetchFn(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    return r.json().catch(() => ({}));
  };
  return { call, send: (chat_id, text, extra = {}) => call("sendMessage", { chat_id, text, ...extra }) };
}

const planKb = () => ({ inline_keyboard: Object.entries(PLANS).map(([k, [n, p]]) => [{ text: `${n} $${p}`, callback_data: `plan:${k}` }]) });
const methodKb = (plan) => ({ inline_keyboard: Object.entries(METHODS).map(([m, [n]]) => [{ text: n, callback_data: `pay:${plan}:${m}` }]) });

async function setPayFor(env, chat, id) {
  await env.ORDERS.put(`p:${chat}`, id || "", { expirationTtl: 3600 });
}

export async function handleUpdate(u, env, tg, fetchFn = fetch) {
  const admin = String(env.ADMIN_CHAT_ID || "");
  if (u.callback_query) return onButton(u.callback_query, env, tg, admin);
  const m = u.message;
  if (!m) return;
  const chat = m.chat.id;
  const text = (m.text || "").trim();
  if (text.startsWith("/")) {
    const [cmdRaw, ...args] = text.split(/\s+/);
    const cmd = cmdRaw.split("@")[0];
    console.log(`incoming ${cmd} from chat ${chat}`);
    if (cmd === "/start") return tg.send(chat, WELCOME);
    if (cmd === "/order") return tg.send(chat, "Which plan?", { reply_markup: planKb() });
    if (cmd === "/pay") return onPay(env, tg, chat, args[0]);
    if (cmd === "/upload") {
      const paid = await openFor(env, chat, "paid");
      return tg.send(chat, paid.length ? `Send your data file now as a document (max ${MAX_DATA_BYTES >> 20} MB).` : "No paid order is waiting for data. Send /order first, then /pay after you have paid.");
    }
    if ((cmd === "/confirm" || cmd === "/reject") && String(chat) === admin && admin) return onAdmin(env, tg, chat, cmd, args[0]);
    return;
  }
  if (m.document) return onData(env, tg, chat, m.document, admin, fetchFn);
  if (text || m.photo) return onProof(env, tg, chat, m, admin);
}

async function onButton(q, env, tg, admin) {
  const chat = q.message.chat.id;
  await tg.call("answerCallbackQuery", { callback_query_id: q.id });
  const [kind, a, b] = (q.data || "").split(":");
  const edit = (text, extra = {}) => tg.call("editMessageText", { chat_id: chat, message_id: q.message.message_id, text, ...extra });
  if (kind === "plan" && PLANS[a]) return edit("How do you want to pay?", { reply_markup: methodKb(a) });
  if (kind === "pay" && PLANS[a] && METHODS[b]) {
    const id = newId();
    const [name, price] = PLANS[a];
    await save(env, id, { chat, plan: a, method: b, state: "awaiting_payment", created: Date.now(), proof: null, data_bytes: null });
    const target = paymentTarget(env, b);
    const pay = target ? `Pay here (${METHODS[b][0]}): ${target}` : PLACEHOLDER;
    if (!target && admin) await tg.send(admin, `Order ${id} (${name} $${price}, ${METHODS[b][0]}) has no payment link configured: send it to chat ${chat}.`);
    return edit(`Order ${id}: ${name} $${price}\n${pay}\nThen send /pay ${id} and your payment screenshot or transaction ID.`);
  }
}

async function onPay(env, tg, chat, idArg) {
  const open = await openFor(env, chat, "awaiting_payment");
  const id = idArg && open.includes(idArg) ? idArg : open[open.length - 1];
  if (!id) return tg.send(chat, "No order waiting for payment. Send /order first.");
  await setPayFor(env, chat, id);
  return tg.send(chat, `Order ${id}: send your payment screenshot or transaction ID now. Training starts only after the payment is confirmed.`);
}

async function onProof(env, tg, chat, m, admin) {
  const id = await env.ORDERS.get(`p:${chat}`);
  if (!id) return;
  let o;
  try {
    o = await load(env, id);
    if (o.chat !== chat) throw new OrderError("not your order");
    const proof = (m.text || (m.photo ? `photo:${m.photo[m.photo.length - 1].file_unique_id}` : "")).trim();
    if (proof.length < 6) throw new OrderError("proof too short");
    o.proof = proof.slice(0, 500);
    await move(env, id, o, "payment_submitted");
  } catch (e) {
    if (e instanceof OrderError) return tg.send(chat, `Cannot accept that: ${e.message}`);
    throw e;
  }
  await setPayFor(env, chat, "");
  await tg.send(chat, "Received. You will get a message when the payment is confirmed.");
  if (admin) {
    if (m.photo) await tg.call("sendPhoto", { chat_id: admin, photo: m.photo[m.photo.length - 1].file_id });
    await tg.send(admin, `Order ${id} payment proof: ${o.proof}\n/confirm ${id}  or  /reject ${id}`);
  }
}

async function onAdmin(env, tg, chat, cmd, id) {
  try {
    const o = await load(env, id || "");
    await move(env, id, o, cmd === "/confirm" ? "paid" : "rejected");
    if (cmd === "/confirm") await tg.send(o.chat, `Payment confirmed for ${id}. Now send your data file as a document (max ${MAX_DATA_BYTES >> 20} MB).`);
    else await tg.send(o.chat, `Payment for ${id} could not be verified. Send /order to try again.`);
    return tg.send(chat, cmd === "/confirm" ? "Confirmed." : "Rejected.");
  } catch (e) {
    if (e instanceof OrderError) return tg.send(chat, `No: ${e.message}`);
    throw e;
  }
}

async function onData(env, tg, chat, doc, admin, fetchFn) {
  const paid = await openFor(env, chat, "paid");
  if (!paid.length) return tg.send(chat, "No paid order is waiting for data.");
  const id = paid[paid.length - 1];
  const o = await load(env, id);
  try {
    const size = doc.file_size || 0;
    if (!(size > 0 && size <= MAX_DATA_BYTES)) throw new OrderError("data file empty or over the size limit");
    await move(env, id, o, "data_received");
    const f = await tg.call("getFile", { file_id: doc.file_id });
    const dl = await fetchFn(`https://api.telegram.org/file/bot${env.TELEGRAM_BOT_TOKEN}/${f.result.file_path}`);
    if (!dl.ok) throw new OrderError("download failed");
    const data = await dl.arrayBuffer();
    if (!env.UPLOAD_PUT_TEMPLATE || !env.UPLOAD_GET_TEMPLATE) throw new OrderError("data storage not configured");
    const rnd = [...crypto.getRandomValues(new Uint8Array(8))].map((x) => x.toString(16).padStart(2, "0")).join("");
    const key = `${id}-${rnd}`;
    const put = await fetchFn(env.UPLOAD_PUT_TEMPLATE.replace("{key}", key), { method: "PUT", body: data });
    if (!put.ok) throw new OrderError("upload failed");
    await move(env, id, o, "training");
    const d = await fetchFn(`https://api.github.com/repos/${env.PIPELINE_REPO}/actions/workflows/finetune.yml/dispatches`, {
      method: "POST",
      headers: { authorization: `Bearer ${env.PIPELINE_TOKEN}`, accept: "application/vnd.github+json", "user-agent": "order-bot", "content-type": "application/json" },
      body: JSON.stringify({ ref: env.PIPELINE_REF || "Main", inputs: { plan: o.plan, data_url: env.UPLOAD_GET_TEMPLATE.replace("{key}", key), order_id: id, telegram_chat_id: String(chat), dry_run: env.DRY_RUN || "1" } }),
    });
    if (!d.ok) throw new OrderError("could not start the pipeline");
  } catch (e) {
    if (!(e instanceof OrderError)) console.log("start failed", e && e.name);
    if (o.state === "data_received" || o.state === "training") { o.state = "paid"; await save(env, id, o); } // retry without paying again
    if (admin) await tg.send(admin, `Order ${id} failed to start: ${e instanceof OrderError ? e.message : "error"}`);
    return tg.send(chat, "Could not start your job. Support has been told.");
  }
  return tg.send(chat, `Data received. Order ${id} is running; you will get the link here when it is done.`);
}

export async function webhookSecret(token) {
  return "hook" + token.slice(-10).replace(/[^A-Za-z0-9_-]/g, "_"); // the webhook secret Telegram echoes back on every update
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const token = env.TELEGRAM_BOT_TOKEN;
    if (!token) return new Response("TELEGRAM_BOT_TOKEN is not set", { status: 500 });
    // GET /setup?t=<last 10 characters of the bot token>: registers this worker's URL as the bot's webhook (and says Bot is online)
    if (request.method === "GET" && url.pathname === "/setup") {
      if (url.searchParams.get("t") !== token.slice(-10)) return new Response("forbidden", { status: 403 });
      const tg = makeTg(env);
      const r = await tg.call("setWebhook", { url: `${url.origin}/hook`, secret_token: await webhookSecret(token), allowed_updates: ["message", "callback_query"] });
      if (r.ok && env.ADMIN_CHAT_ID) await tg.send(env.ADMIN_CHAT_ID, "Bot is online");
      return new Response(JSON.stringify({ ok: !!r.ok, description: r.description || "webhook set" }), { headers: { "content-type": "application/json" } });
    }
    if (request.method === "POST" && url.pathname === "/hook") {
      if (request.headers.get("x-telegram-bot-api-secret-token") !== (await webhookSecret(token))) return new Response("forbidden", { status: 403 });
      const update = await request.json().catch(() => null);
      if (update) ctx.waitUntil(handleUpdate(update, env, makeTg(env)).catch((e) => console.log("update failed", e && e.name)));
      return new Response("ok");
    }
    return new Response("ok");
  },
};
