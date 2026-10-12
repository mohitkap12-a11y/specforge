import json

from specforge.core import config
from specforge.core.db import rows


class Fake:
    name = "fake"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def generate(self, messages, **kw):
        self.calls += 1
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return r(messages) if callable(r) else r

    def list_models(self): return []
    def health(self): return {"ok": True}
    def embed(self, t, **kw): return []


def settings():
    s = config.deep_merge(config.DEFAULT_SETTINGS, {})
    s["tasks"]["generate"]["model"] = "fake-model"
    return s


def reply_for(conn):
    req = next(r for r in rows(conn, "SELECT * FROM catalog_item WHERE kind='requirement'") if "Invalid Field in Command if a reserved" in r["label"])
    text = json.loads(req["attrs_json"])["text"]
    step = {"command": "cmd:admin:06", "fields": [{"dw": 10, "hi": 15, "lo": 8, "op": "ne", "value": 0}]}
    base = {"title": "Identify with reserved CDW10[15:08] set", "observability": "stimulus", "steps": [step], "quotes": [text[:60]]}
    return req, base


