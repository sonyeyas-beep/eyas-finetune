import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from orders import MAX_DATA_BYTES, OrderError, Orders, payment_target  # noqa: E402

DENY = re.compile(r"atum|mlin|engine|tile|worker|amx|proc\.rs|runner_job|ci_finetune|stream_bench|llama|mistral|qwen|gemma", re.I)


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
        with self.assertRaises(OrderError):
            payment_target("card", {})
        self.assertEqual(payment_target("usdt", {"USDT_WALLET": "w"}), "w")
        self.assertNotRegex((ROOT / "bot.py").read_text() + (ROOT / "orders.py").read_text(), r"https?://(buy\.stripe|paypal\.me|pay)")


class Public(unittest.TestCase):
    def test_nothing_about_the_private_side_is_in_the_public_tree(self):
        for p in ROOT.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts and p.name != "test_all.py":
                t = p.read_text(errors="ignore")
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
            self.assertEqual(d[True]["workflow_dispatch"]["inputs"]["plan"]["options"], ["basic_7B", "standard_13B", "premium_70B"])
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
