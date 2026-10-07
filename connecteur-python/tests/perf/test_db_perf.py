"""Tests perf DB : comptage requetes + usage index (EXPLAIN)."""
import pytest

from .helpers import assert_under

pytestmark = pytest.mark.perf

_TICK_QUERIES = [
    ("push", "SELECT * FROM invoices WHERE source IN ('web', 'pos') AND synced_sage = 0 "
             "AND statut != 'brouillon' ORDER BY date_facture ASC"),
    ("certify", "SELECT * FROM invoices WHERE source = 'pos' AND synced_sage = 0 "
                "ORDER BY date_facture ASC"),
    ("contacts", "SELECT * FROM contacts WHERE synced_sage = 0 ORDER BY code"),
]


def test_db_query_count(client, query_counter, thresholds):
    query_counter["n"] = 0
    resp = client.get("/billing")
    assert resp.status_code == 200
    assert query_counter["n"] <= thresholds["db"]["max_queries_per_request"], \
        "GET /billing: {} requetes SQLite".format(query_counter["n"])


def test_index_usage():
    from app.storage.db import get_cursor

    with get_cursor() as cur:
        for name, sql in _TICK_QUERIES:
            cur.execute("EXPLAIN QUERY PLAN " + sql)
            plan = " | ".join(str(r[3] if len(r) > 3 else r[0]) for r in cur.fetchall())
            assert "USING INDEX" in plan, "{}: pas d'index -> {}".format(name, plan)
            assert "SCAN " not in plan.replace("SEARCH", ""), "{}: SCAN -> {}".format(name, plan)
