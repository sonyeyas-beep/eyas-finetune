# Running the bot without a server

Pick ONE of the two. Do not run both: the Render version polls Telegram and removes the webhook the Cloudflare version needs.

## A. Cloudflare Workers (free, always on) - 3 clicks

1. Cloudflare dashboard -> Workers & Pages -> Create -> **Import a repository** -> pick this repository (branch `Main`) -> **Deploy**.
2. Open the new Worker -> Settings -> **Variables and secrets** -> add `TELEGRAM_BOT_TOKEN` and `ADMIN_CHAT_ID` (add `STRIPE_LINK`, `PAYPAL_LINK`, `USDT_WALLET` when you have them; without them /order answers "Payment link will be sent after you confirm" and tells you to send the link) -> Deploy.
3. Open `https://<your-worker>.workers.dev/setup?t=<the last 10 characters of the bot token>` once. It answers `{"ok":true,...}`, registers the webhook and sends "Bot is online" to `ADMIN_CHAT_ID`.

The same registration as one Telegram API call instead (open it in a browser; fill in `TOKEN`, the worker address, and `LAST10` = the last 10 characters of the token):

```
https://api.telegram.org/botTOKEN/setWebhook?url=https://YOUR-WORKER.workers.dev/hook&secret_token=hookLAST10
```
The secret is required: without it anyone could post a fake update and pretend to be you.

The Worker keeps orders in a KV namespace called `ORDERS`; the deploy creates it from `wrangler.jsonc`. If the dashboard says the binding is missing, Settings -> Bindings -> Add -> KV namespace -> variable name `ORDERS`.
Data upload and the pipeline need `PIPELINE_REPO`, `PIPELINE_TOKEN`, `UPLOAD_PUT_TEMPLATE`, `UPLOAD_GET_TEMPLATE` as well (the order flow works without them; the upload step then says "Could not start your job").

## B. Render (free fallback, one Blueprint) - 3 clicks

1. Render dashboard -> New -> **Blueprint** -> pick this repository (branch `Main`).
2. Fill the environment form it shows (`TELEGRAM_BOT_TOKEN`, `ADMIN_CHAT_ID`, the payment links if you have them).
3. **Apply**. The bot starts, deletes any webhook, polls Telegram and sends "Bot is online".

The free Render web service sleeps after 15 minutes without web traffic and loses `orders.json` on a restart: fine for a test, not for real orders. Cloudflare keeps its orders in KV.
