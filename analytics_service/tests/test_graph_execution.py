"""Graph selection cannot expand the authorized physical schema."""
import copy
from types import SimpleNamespace

import pytest

from pipeline import text2sql_falkordb as p


def test_execution_uses_graph_and_preserves_deployed_types(monkeypatch):
    physical = p.fetch_catalog_tables()
    knowledge = copy.deepcopy(physical)
    knowledge["Transaction"]["foreign_keys"] = [
        {"column": "account_id", "type": "text", "ref_table": "AccountPartyLink"},
        {"column": "secret", "type": "text", "ref_table": "Party"},
    ]
    knowledge["Transaction"]["fields"].append({"name": "invented", "type": "STRING"})
    graph = object()
    monkeypatch.setattr(p, "connect_graph", lambda: graph)
    monkeypatch.setattr(p, "fetch_all_tables", lambda g: knowledge)
    seen = []

    def retrieve(g, question, top_k, tables):
        assert g is graph
        assert set(tables) == {"Transaction", "AccountPartyLink"}
        assert len(tables["Transaction"]["foreign_keys"]) == 1
        seen.append(question)
        tables["Transaction"]["fields"] = []
        return {"Transaction": tables["Transaction"]}

    monkeypatch.setattr(p, "retrieve_relevant_tables", retrieve)
    result = p.retrieve_execution_tables("payments?", 3, {
        "Transaction": ["account_id", "normalized_booked_amount"],
        "AccountPartyLink": ["account_id", "party_id"],
    })
    assert seen == ["payments?"]
    fields = result["Transaction"]["fields"]
    assert {f["name"] for f in fields} == {"account_id", "normalized_booked_amount"}
    assert next(f for f in fields if f["name"] == "normalized_booked_amount")["sql_type"].startswith("STRUCT")


def test_graph_outage_never_falls_back(monkeypatch):
    def unavailable():
        raise RuntimeError("unavailable")
    monkeypatch.setattr(p, "connect_graph", unavailable)
    monkeypatch.setattr(p, "generate_sql_ollama", lambda *a, **kw: pytest.fail("Must not generate"))
    with pytest.raises(RuntimeError, match="unavailable"):
        p.generate_sql_kag("payments?", execution_mode=True)


def test_empty_graph_fails_closed(monkeypatch):
    monkeypatch.setattr(p, "connect_graph", object)
    monkeypatch.setattr(p, "fetch_all_tables", lambda g: {})
    with pytest.raises(RuntimeError, match="No authorized"):
        p.retrieve_execution_tables("payments?", 3, None)


def test_graph_hop_cannot_add_unapproved_table(monkeypatch):
    tables = p.fetch_catalog_tables()
    tables = {"Transaction": tables["Transaction"], "AccountPartyLink": tables["AccountPartyLink"]}
    monkeypatch.setattr(p.semantic_search, "embed", lambda texts: [1, 1] if isinstance(texts, list) else 1)
    monkeypatch.setattr(p.semantic_search, "cosine_sim", lambda a, b: 1)
    calls = []

    class Graph:
        def query(self, query, parameters):
            calls.append(query)
            return SimpleNamespace(result_set=[["AccountPartyLink"], ["SecretTable"]])

    result = p.retrieve_relevant_tables(Graph(), "Transaction direction", top_k=1, tables=tables)
    assert calls and "REFERENCES|LINKS_TO" in calls[0]
    assert "AccountPartyLink" in result
    assert "SecretTable" not in result
