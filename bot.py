"""Order bot: plans -> payment method -> payment link -> proof -> operator confirms -> data upload -> pipeline dispatch.
Everything private comes from the environment; nothing here knows what runs inside the pipeline."""
import asyncio
import logging
import os
import secrets

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from orders import MAX_DATA_BYTES, METHODS, PLANS, OrderError, Orders, payment_target

log = logging.getLogger("bot")
ADMIN = int(os.environ.get("ADMIN_CHAT_ID", "0"))
PAY_NAMES = {"card": "Card", "paypal": "PayPal", "usdt": "USDT"}
WELCOME = "Welcome! Plans:\n" + "\n".join(f"- {n} ${p}" for n, p in PLANS.values()) + "\n\nHow do you want to pay?\n1. Card\n2. PayPal\n3. USDT\n\nSend /order to start."


def book(ctx) -> Orders:
    return ctx.application.bot_data["orders"]


def plan_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"{n} ${p}", callback_data=f"plan:{k}")] for k, (n, p) in PLANS.items()])


def method_kb(plan):
    return InlineKeyboardMarkup([[InlineKeyboardButton(PAY_NAMES[m], callback_data=f"pay:{plan}:{m}")] for m in METHODS])


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)


async def order(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Which plan?", reply_markup=plan_kb())


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    kind, *rest = q.data.split(":")
    if kind == "plan" and rest and rest[0] in PLANS:
        await q.edit_message_text("How do you want to pay?", reply_markup=method_kb(rest[0]))
    elif kind == "pay" and len(rest) == 2:
        try:
            target = payment_target(rest[1])
            oid = book(ctx).create(q.message.chat_id, rest[0], rest[1])
        except OrderError as e:
            await q.edit_message_text(f"Sorry: {e}")
            return
        name, price = PLANS[rest[0]]
        await q.edit_message_text(f"Order {oid}: {name} ${price}\nPay here ({PAY_NAMES[rest[1]]}): {target}\nThen send /pay {oid} and your payment screenshot or transaction ID.")


async def pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat.id
    if ctx.args:
        ctx.user_data["pay_for"] = ctx.args[0]
    open_ = book(ctx).open_for(chat, "awaiting_payment")
    if not ctx.user_data.get("pay_for") or ctx.user_data["pay_for"] not in open_:
        ctx.user_data["pay_for"] = open_[-1] if open_ else None
    if not ctx.user_data["pay_for"]:
        await update.message.reply_text("No order waiting for payment. Send /order first.")
        return
    await update.message.reply_text(f"Order {ctx.user_data['pay_for']}: send your payment screenshot or transaction ID now. Training starts only after the payment is confirmed.")


async def on_proof(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat.id
    oid = ctx.user_data.get("pay_for")
    if not oid:
        return
    m = update.message
    proof = m.text if m.text else (f"photo:{m.photo[-1].file_unique_id}" if m.photo else None)
    try:
        book(ctx).submit_proof(oid, chat, proof)
    except OrderError as e:
        await m.reply_text(f"Cannot accept that: {e}")
        return
    ctx.user_data["pay_for"] = None
    await m.reply_text("Received. You will get a message when the payment is confirmed.")
    if ADMIN:
        if m.photo:
            await ctx.bot.send_photo(ADMIN, m.photo[-1].file_id)
        await ctx.bot.send_message(ADMIN, f"Order {oid} payment proof: {proof}\n/confirm {oid}  or  /reject {oid}")


def admin_only(fn):
    async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ADMIN or update.effective_chat.id != ADMIN:
            return
        await fn(update, ctx)
    return w


@admin_only
async def confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        oid = ctx.args[0]
        book(ctx).confirm_payment(oid)
    except (IndexError, OrderError) as e:
        await update.message.reply_text(f"No: {e}")
        return
    await ctx.bot.send_message(book(ctx).get(oid)["chat"], f"Payment confirmed for {oid}. Now send your data file as a document (max {MAX_DATA_BYTES >> 20} MB).")
    await update.message.reply_text("Confirmed.")


@admin_only
async def reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        oid = ctx.args[0]
        book(ctx).reject(oid)
    except (IndexError, OrderError) as e:
        await update.message.reply_text(f"No: {e}")
        return
    await ctx.bot.send_message(book(ctx).get(oid)["chat"], f"Payment for {oid} could not be verified. Send /order to try again.")


async def store_upload(data: bytes, key: str) -> str:
    """PUT to a pre-signed URL (UPLOAD_PUT_TEMPLATE) and return the short-lived GET URL (UPLOAD_GET_TEMPLATE); both contain {key}."""
    put, get = os.environ.get("UPLOAD_PUT_TEMPLATE"), os.environ.get("UPLOAD_GET_TEMPLATE")
    if not put or not get:
        raise OrderError("data storage not configured")
    async with httpx.AsyncClient(timeout=600) as c:
        r = await c.put(put.format(key=key), content=data)
        r.raise_for_status()
    return get.format(key=key)


async def dispatch(order_id: str, plan: str, data_url: str, chat: int):
    repo, tok = os.environ["PIPELINE_REPO"], os.environ["PIPELINE_TOKEN"]
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"https://api.github.com/repos/{repo}/actions/workflows/finetune.yml/dispatches",
            headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
            json={"ref": os.environ.get("PIPELINE_REF", "Main"), "inputs": {"plan": plan, "data_url": data_url, "order_id": order_id, "telegram_chat_id": str(chat), "dry_run": os.environ.get("DRY_RUN", "1")}},
        )
        r.raise_for_status()


async def on_data(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat, doc = update.effective_chat.id, update.message.document
    paid = book(ctx).open_for(chat, "paid")
    if not paid:
        await update.message.reply_text("No paid order is waiting for data.")
        return
    oid = paid[-1]
    try:
        book(ctx).accept_data(oid, chat, doc.file_size or 0)
        tg_file = await doc.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        url = await store_upload(data, f"{oid}-{secrets.token_hex(8)}")
        book(ctx).start_training(oid)
        await dispatch(oid, book(ctx).get(oid)["plan"], url, chat)
    except (OrderError, httpx.HTTPError) as e:
        book(ctx).rewind(oid)
        log.warning("order %s failed to start: %s", oid, type(e).__name__)
        await update.message.reply_text("Could not start your job. Support has been told.")
        if ADMIN:
            await ctx.bot.send_message(ADMIN, f"Order {oid} failed to start: {type(e).__name__}")
        return
    await update.message.reply_text(f"Data received. Order {oid} is running; you will get the link here when it is done.")


def token() -> str:
    t = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
    if not t:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    return t


async def log_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    if m and m.text and m.text.startswith("/"):
        log.info("incoming %s from chat %s", m.text.split()[0].split("@")[0], update.effective_chat.id)


async def on_start(app: Application):
    """Polling and a webhook cannot coexist: drop any webhook first, then tell the operator the bot is up."""
    await app.bot.delete_webhook(drop_pending_updates=False)
    me = await app.bot.get_me()
    log.info("bot online as @%s, polling (webhook removed), admin chat %s", me.username, ADMIN or "NOT SET")
    if ADMIN:
        try:
            await app.bot.send_message(ADMIN, "Bot is online")
        except Exception as e:  # the admin has not pressed Start in this bot yet
            log.warning("could not tell the admin: %s", type(e).__name__)


def health_server():
    """Platforms that expect a listening port (Render) get a trivial 200 here; polling does not need it."""
    port = os.environ.get("PORT")
    if not port:
        return
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    threading.Thread(target=HTTPServer(("0.0.0.0", int(port)), H).serve_forever, daemon=True).start()
    log.info("health endpoint on port %s", port)


def build() -> Application:
    app = Application.builder().token(token()).post_init(on_start).build()
    app.bot_data["orders"] = Orders(os.environ.get("ORDERS_FILE", "orders.json"))
    app.add_handler(MessageHandler(filters.COMMAND, log_command), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("order", order))
    app.add_handler(CommandHandler("pay", pay))
    app.add_handler(CommandHandler("confirm", confirm))
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL, on_data))
    app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.PHOTO, on_proof))
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request lines contain the token
    health_server()
    build().run_polling(allowed_updates=Update.ALL_TYPES)
