"""Order book and payment gate (no Telegram, no network): the part that decides when training may start."""
import json
import os
import re
import secrets
import time
from pathlib import Path

PLANS = {"basic_7B": ("Basic 7B", 100), "standard_13B": ("Standard 13B", 200), "premium_70B": ("Premium 70B", 400)}
METHODS = {"card": "STRIPE_LINK", "paypal": "PAYPAL_LINK", "usdt": "USDT_WALLET"}
STATES = ("awaiting_payment", "payment_submitted", "paid", "data_received", "training", "done", "failed", "rejected")
_NEXT = {
    "awaiting_payment": {"payment_submitted", "rejected"},
    "payment_submitted": {"paid", "rejected"},
    "paid": {"data_received"},
    "data_received": {"training"},
    "training": {"done", "failed"},
}
MAX_DATA_BYTES = 20 << 20  # the bot API's own download limit for files
ORDER_ID = re.compile(r"^[A-Za-z0-9_-]{3,40}$")


class OrderError(Exception):
    pass


class Orders:
    def __init__(self, path):
        self.path = Path(path)
        self.d = json.loads(self.path.read_text()) if self.path.exists() else {}

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d))
        os.replace(tmp, self.path)

    def create(self, chat_id, plan, method):
        if plan not in PLANS:
            raise OrderError("unknown plan")
        if method not in METHODS:
            raise OrderError("unknown payment method")
        oid = "o" + secrets.token_hex(6)
        self.d[oid] = {"chat": int(chat_id), "plan": plan, "method": method, "state": "awaiting_payment", "created": int(time.time()), "proof": None, "data_bytes": None}
        self._save()
        return oid

    def get(self, oid):
        if oid not in self.d:
            raise OrderError("no such order")
        return self.d[oid]

    def _move(self, oid, to):
        o = self.get(oid)
        if to not in _NEXT.get(o["state"], ()):
            raise OrderError(f"cannot go from {o['state']} to {to}")
        o["state"] = to
        self._save()

    def open_for(self, chat_id, *states):
        return [k for k, o in self.d.items() if o["chat"] == int(chat_id) and o["state"] in states]

    def submit_proof(self, oid, chat_id, proof):
        o = self.get(oid)
        if o["chat"] != int(chat_id):
            raise OrderError("not your order")
        proof = (proof or "").strip()
        if len(proof) < 6:
            raise OrderError("proof too short")
        o["proof"] = proof[:500]
        self._move(oid, "payment_submitted")

    def confirm_payment(self, oid):
        """Only the operator's confirmation moves an order to `paid`; nothing the client sends can."""
        self._move(oid, "paid")

    def reject(self, oid):
        self._move(oid, "rejected")

    def accept_data(self, oid, chat_id, size):
        o = self.get(oid)
        if o["chat"] != int(chat_id):
            raise OrderError("not your order")
        if o["state"] != "paid":
            raise OrderError("payment is not confirmed")
        if not 0 < size <= MAX_DATA_BYTES:
            raise OrderError("data file empty or over the size limit")
        o["data_bytes"] = size
        self._move(oid, "data_received")

    def start_training(self, oid):
        o = self.get(oid)
        if o["state"] != "data_received":
            raise OrderError("not ready to train")
        self._move(oid, "training")

    def rewind(self, oid):
        """A start that failed before anything ran: back to `paid` so the client can send the file again."""
        o = self.get(oid)
        if o["state"] in ("data_received", "training"):
            o["state"] = "paid"
            self._save()

    def finish(self, oid, ok):
        self._move(oid, "done" if ok else "failed")


def payment_target(method, env=os.environ):
    v = env.get(METHODS[method], "")
    if not v:
        raise OrderError("payment method not configured")
    return v
