from __future__ import annotations
from dataclasses import dataclass
from stock_analysis.cloud_store import SupabaseStore
from stock_analysis.models import PaperOrder, Security
@dataclass
class Response:
    data: object
class FakeQuery:
    def __init__(self, response: Response | None = None):
        self.response = response or Response([])
        self.calls: list[tuple[str, object]] = []
    def select(self, value: str):
        self.calls.append(("select", value))
        return self
    def eq(self, key: str, value: object):
        self.calls.append(("eq", (key, value)))
        return self
    def order(self, key: str, desc: bool = False):
        self.calls.append(("order", (key, desc)))
        return self
    def limit(self, value: int):
        self.calls.append(("limit", value))
        return self
    def upsert(self, payload: dict, on_conflict: str | None = None, **kwargs):
        self.calls.append(("upsert", (payload, on_conflict, kwargs)))
        return self
    def insert(self, payload: dict):
        self.calls.append(("insert", payload))
        return self
    def update(self, payload: dict):
        self.calls.append(("update", payload))
        return self
    def delete(self):
        self.calls.append(("delete", None))
        return self
    def execute(self):
        return self.response
class FakeClient:
    def __init__(self):
        self.tables: dict[str, FakeQuery] = {}
        self.rpc_calls: list[tuple[str, dict]] = []
    def table(self, name: str) -> FakeQuery:
        return self.tables.setdefault(name, FakeQuery())
    def rpc(self, name: str, params: dict) -> FakeQuery:
        self.rpc_calls.append((name, params))
        return FakeQuery(Response(123))
def test_supabase_store_always_writes_authenticated_user_id():
    client = FakeClient()
    store_a = SupabaseStore(client, "user-a")
    store_b = SupabaseStore(client, "user-b")
    security = Security("600519", "maotai", "SSE")
    store_a.save_watchlist_item("ignored-account", security)
    store_b.save_watchlist_item("ignored-account", security)
    payloads = [call[1][0] for call in client.tables["user_watchlist"].calls if call[0] == "upsert"]
    assert payloads[0]["user_id"] == "user-a"
    assert payloads[1]["user_id"] == "user-b"
def test_supabase_order_uses_atomic_rpc_and_does_not_trust_new_cash():
    client = FakeClient()
    store = SupabaseStore(client, "user-a")
    order = PaperOrder(id=None, account_id="user-a", symbol="SSE.600519", side="\u4e70\u5165", shares=100, price=10.0, fee=5.0, traded_at="2026-08-31")
    order_id = store.record_order_and_update_cash(order, new_cash=1.0)
    assert order_id == 123
    name, params = client.rpc_calls[0]
    assert name == "record_paper_order"
    assert params["p_cash_delta"] == -1005.0
    assert "p_new_cash" not in params
