import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from orders import MAX_DATA_BYTES, OrderError, Orders, payment_target  # noqa: E402

DENY = re.compile(r"atum|mlin|engine|tile|amx|proc\.rs|runner_job|ci_finetune|stream_bench|llama|mistral|qwen|gemma", re.I)


class Book(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.f = Path(tempfile.mkdtemp()) / "o.json"
        self.o = Orders(self.f)

    def test_training_cannot_start_before_the_operator_confirms(self):
        oid = self.o.create(5, "basic_7B", "card")
        with self.assertRaises(OrderError):
            self.o.accept_data(oid, 5, 100)
        with self.assertRaises(OrderError):
            self.o.confirm_payment(oid)  # no proof yet
        self.o.submit_proof(oid, 5, "txid-123456")
        with self.assertRaises(OrderError):
            self.o.accept_data(oid, 5, 100)  # proof is not payment
        with self.assertRaises(OrderError):
            self.o.start_training(oid)
        self.o.confirm_payment(oid)
        self.o.accept_data(oid, 5, 100)
        self.o.start_training(oid)
        self.o.finish(oid, True)
        self.assertEqual(self.o.get(oid)["state"], "done")

    def test_other_chat_cannot_touch_the_order_and_limits_hold(self):
        oid = self.o.create(5, "premium_70B", "usdt")
        with self.assertRaises(OrderError):
            self.o.submit_proof(oid, 6, "txid-123456")
        self.o.submit_proof(oid, 5, "txid-123456")
        self.o.confirm_payment(oid)
        for chat, size in ((6, 10), (5, 0), (5, MAX_DATA_BYTES + 1)):
            with self.assertRaises(OrderError):
                self.o.accept_data(oid, chat, size)
        with self.assertRaises(OrderError):
            self.o.create(5, "tiny", "card")
        with self.assertRaises(OrderError):
            self.o.create(5, "basic_7B", "cash")

    def test_a_failed_start_can_be_retried_without_paying_again(self):
        oid = self.o.create(5, "basic_7B", "card")
        self.o.submit_proof(oid, 5, "txid-123456")
        self.o.confirm_payment(oid)
        self.o.accept_data(oid, 5, 100)
        self.o.rewind(oid)
        self.o.accept_data(oid, 5, 100)
        self.o.start_training(oid)
        self.o.rewind(oid)
        self.assertEqual(self.o.get(oid)["state"], "paid")

    def test_rejected_and_persisted(self):
        oid = self.o.create(5, "standard_13B", "paypal")
        self.o.submit_proof(oid, 5, "screenshot-abc")
        self.o.reject(oid)
        with self.assertRaises(OrderError):
            self.o.confirm_payment(oid)
        self.assertEqual(Orders(self.f).get(oid)["state"], "rejected")
        ids = {self.o.create(1, "basic_7B", "card") for _ in range(50)}
        self.assertEqual(len(ids), 50)

    def test_links_come_from_the_environment_only(self):
        self.assertEqual(payment_target("card", {}), "")  # not configured: the bot says the link follows, it does not look broken
        self.assertEqual(payment_target("usdt", {"USDT_WALLET": "w"}), "w")
        self.assertNotRegex((ROOT / "bot.py").read_text() + (ROOT / "orders.py").read_text(), r"https?://(buy\.stripe|paypal\.me|pay)")


class Public(unittest.TestCase):
    def test_nothing_about_the_private_side_is_in_the_public_tree(self):
        for p in ROOT.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts and "samples" not in p.parts and p.name != "test_all.py":
                t = p.read_text(errors="ignore").replace("'atum-v3'", "")  # the one branch name the pipeline must name to check out the tools (TOOLS_REF overrides it)
                self.assertIsNone(DENY.search(p.name + t), f"{p}: {DENY.search(p.name + t)}")

    def test_the_order_id_rule_accepts_test_ids_and_bot_ids_and_nothing_with_shell_characters(self):
        w = (ROOT / ".github/workflows/finetune.yml").read_text()
        m = re.search(r'ORDER_ID" =~ (\^\[A-Za-z0-9_-\]\{\d+,\d+\}\$)', w)
        self.assertTrue(m, "order id rule missing")
        rx = re.compile(m.group(1))
        from orders import ORDER_ID
        for good in ("test1", "o1a2b3c4d5e6f", "abc"):
            self.assertTrue(rx.match(good) and ORDER_ID.match(good), good)
        for bad in ("ab", "a b", "a;rm", "$(x)", "a" * 41, "x\n"):
            self.assertFalse(rx.match(bad), repr(bad))
        o = Orders(Path(__import__("tempfile").mkdtemp()) / "o.json")
        self.assertTrue(ORDER_ID.match(o.create(1, "basic_7B", "card")))

    def test_workflow_is_safe(self):
        w = (ROOT / ".github/workflows/finetune.yml").read_text()
        for must in ("workflow_dispatch", "plan", "data_url", "order_id", "telegram_chat_id", "secrets.GH_PAT", "persist-credentials: false", "add-mask"):
            self.assertIn(must, w)
        # untrusted inputs reach shell only through env, never interpolated into a script body
        for m in re.finditer(r"run: \|(.*?)(?=\n      - |\Z)", w, re.S):
            self.assertNotRegex(m.group(1), r"\$\{\{\s*inputs\.", m.group(1)[:80])
        self.assertNotRegex(w, r"ghp_|github_pat_|\b\d{8,}:[A-Za-z0-9_-]{30,}")
        self.assertNotIn("pull_request", w)
        try:
            import yaml
            d = yaml.safe_load(w)
            self.assertEqual(d[True]["workflow_dispatch"]["inputs"]["plan"]["options"], ["basic_7B", "standard_13B", "premium_70B", "proof_0.5B"])
        except ImportError:
            pass

    def test_bot_has_all_commands_and_prices(self):
        b = (ROOT / "bot.py").read_text()
        for c in ('"start"', '"order"', '"pay"', '"confirm"'):
            self.assertIn(c, b)
        from orders import PLANS
        self.assertEqual({k: v[1] for k, v in PLANS.items()}, {"basic_7B": 100, "standard_13B": 200, "premium_70B": 400})
        self.assertNotIn('"ref": "main"', b)  # the default branch is called Main
        compile(b, "bot.py", "exec")


class BotRuns(unittest.TestCase):
    """The handlers answer, the startup drops any webhook and tells the admin, and the token comes from the secret's name."""

    def setUp(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST"
        os.environ["ORDERS_FILE"] = str(Path(__import__("tempfile").mkdtemp()) / "o.json")
        os.environ["ADMIN_CHAT_ID"] = "42"
        os.environ["STRIPE_LINK"] = "https://pay.example/s"
        import importlib
        import bot
        self.bot = importlib.reload(bot)

    def _upd(self, text="/start"):
        from unittest.mock import AsyncMock, MagicMock
        u = MagicMock()
        u.message.reply_text = AsyncMock()
        u.effective_message.text = text
        u.effective_chat.id = 7
        return u

    def test_start_replies_with_the_plans_and_the_payment_question(self):
        import asyncio
        u = self._upd()
        asyncio.run(self.bot.start(u, None))
        text = u.message.reply_text.call_args[0][0]
        for s in ("$100", "$200", "$400", "How do you want to pay?", "1. Card", "2. PayPal", "3. USDT"):
            self.assertIn(s, text)

    def test_order_asks_for_the_plan_then_the_payment_method_then_sends_the_link_from_env(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        u = self._upd("/order")
        asyncio.run(self.bot.order(u, None))
        self.assertEqual(u.message.reply_text.call_args[0][0], "Which plan?")
        kb = u.message.reply_text.call_args[1]["reply_markup"]
        self.assertEqual([b.callback_data for r in kb.inline_keyboard for b in r], ["plan:basic_7B", "plan:standard_13B", "plan:premium_70B"])
        q = MagicMock()
        q.answer, q.edit_message_text = AsyncMock(), AsyncMock()
        q.data, q.message.chat_id = "plan:basic_7B", 7
        up = MagicMock()
        up.callback_query = q
        ctx = MagicMock()
        ctx.application.bot_data = {"orders": self.bot.Orders(os.environ["ORDERS_FILE"])}
        asyncio.run(self.bot.on_button(up, ctx))
        self.assertEqual(q.edit_message_text.call_args[0][0], "How do you want to pay?")
        q.data = "pay:basic_7B:card"
        asyncio.run(self.bot.on_button(up, ctx))
        self.assertIn("https://pay.example/s", q.edit_message_text.call_args[0][0])

    def test_order_without_a_configured_link_answers_with_the_placeholder(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        os.environ.pop("PAYPAL_LINK", None)
        q = MagicMock()
        q.answer, q.edit_message_text = AsyncMock(), AsyncMock()
        q.data, q.message.chat_id = "pay:basic_7B:paypal", 7
        up = MagicMock()
        up.callback_query = q
        ctx = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.application.bot_data = {"orders": self.bot.Orders(os.environ["ORDERS_FILE"])}
        asyncio.run(self.bot.on_button(up, ctx))
        text = q.edit_message_text.call_args[0][0]
        self.assertIn("Payment link will be sent after you confirm", text)
        self.assertNotIn("not configured", text)
        ctx.bot.send_message.assert_awaited()  # the operator is told to send the link

    def test_startup_removes_the_webhook_tells_the_admin_and_commands_are_logged(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock
        app = MagicMock()
        app.bot.delete_webhook, app.bot.send_message = AsyncMock(), AsyncMock()
        app.bot.get_me = AsyncMock(return_value=MagicMock(username="b"))
        asyncio.run(self.bot.on_start(app))
        app.bot.delete_webhook.assert_awaited_once()
        app.bot.send_message.assert_awaited_once_with(42, "Bot is online")
        with self.assertLogs("bot", "INFO") as cm:
            asyncio.run(self.bot.log_command(self._upd("/order extra"), None))
        self.assertIn("incoming /order from chat 7", cm.output[0])
        self.assertEqual(self.bot.token(), "123456:TEST")
        built = self.bot.build()
        self.assertGreaterEqual(len(built.handlers[0]) + len(built.handlers.get(-1, [])), 8)

    def test_deployment_files_exist_and_run_the_bot_with_polling_and_restart(self):
        for f in ("Dockerfile", "docker-compose.yml", "Procfile", "render.yaml", "deploy_bot.sh"):
            self.assertTrue((ROOT / f).exists(), f)
        self.assertIn("restart: always", (ROOT / "docker-compose.yml").read_text())
        d = (ROOT / "deploy_bot.sh").read_text()
        for s in ("Restart=always", "systemctl enable --now", "TELEGRAM_BOT_TOKEN"):
            self.assertIn(s, d)
        self.assertIn("run_polling", (ROOT / "bot.py").read_text())
        self.assertEqual(subprocess.run(["bash", "-n", str(ROOT / "deploy_bot.sh")]).returncode, 0)
        self.assertNotRegex(d + (ROOT / "render.yaml").read_text(), r"\d{8,}:[A-Za-z0-9_-]{30,}")


class NoServer(unittest.TestCase):
    def test_render_and_cloudflare_files_are_complete(self):
        r = (ROOT / "render.yaml").read_text()
        for s in ("runtime: docker", "dockerfilePath: ./Dockerfile", "branch: Main", "TELEGRAM_BOT_TOKEN", "healthCheckPath"):
            self.assertIn(s, r)
        d = (ROOT / "Dockerfile").read_text()
        self.assertIn("USER bot", d)
        self.assertIn('CMD ["python", "bot.py"]', d)
        wr = (ROOT / "wrangler.jsonc").read_text()
        self.assertIn('"main": "worker.js"', wr)
        self.assertIn('"binding": "ORDERS"', wr)
        s = (ROOT / "SETUP.md").read_text()
        self.assertIn("/setup?t=", s)
        self.assertIn("Payment link will be sent after you confirm", s)

    def test_the_javascript_side_passes(self):
        import shutil
        if not shutil.which("node"):
            self.skipTest("node not installed")
        r = subprocess.run(["node", "--test", str(ROOT / "worker.test.mjs")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout[-1500:])


class Private(unittest.TestCase):
    def test_entry_script_refuses_what_the_free_runner_cannot_hold_and_never_fakes_a_model(self):
        s = ROOT.parent.parent / "scripts/ci_finetune.sh"
        if not s.exists():
            self.skipTest("private side not present")
        d = Path(os.environ.get("TMPDIR", "/tmp")) / "cf_t"
        d.mkdir(exist_ok=True)
        data = d / "d.bin"
        data.write_bytes(b"x")
        r = subprocess.run(["bash", str(s), "premium_70B", str(data), str(d / "o")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 4, r.stderr)
        r = subprocess.run(["bash", str(s), "nope", str(data), str(d / "o")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        r = subprocess.run(["bash", str(s), "basic_7B", str(d / "missing"), str(d / "o")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
