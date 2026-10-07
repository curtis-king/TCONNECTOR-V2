"""Socle tests de performance : seuils, mesure, JSONL, compteur requetes.

Reuse les fixtures de tests/conftest.py (app_instance, client authentifie).
Mocks reseau : voir tests/perf/mocks/ (Sage + SFEC en memoire, 0 reseau).
"""
import json
import os
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
THRESHOLDS_PATH = os.path.join(_HERE, "thresholds.json")
RESULTS_PATH = os.path.join(_HERE, "perf-results.jsonl")


@pytest.fixture(scope="session")
def thresholds():
    from .helpers import load_thresholds

    return load_thresholds()


def pytest_runtest_setup(item):
    if "perf" in item.keywords:
        item._perf_start = time.perf_counter()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    if call.when == "call" and "perf" in item.keywords:
        ms = (time.perf_counter() - getattr(item, "_perf_start", time.perf_counter())) * 1000
        rec = {
            "test": item.nodeid,
            "status": outcome.get_result().outcome,
            "duration_ms": round(ms, 2),
            "timestamp": time.time(),
        }
        with open(RESULTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


@pytest.fixture
def query_counter(monkeypatch):
    """Compte les execute() SQLite via proxy, sur tous les modules consommateurs."""
    import importlib
    from contextlib import contextmanager

    import app.storage.db as sqlite_db

    state = {"n": 0}
    _real = sqlite_db.get_cursor

    class _CountingCursor:
        def __init__(self, cur):
            self._cur = cur

        def execute(self, *a, **k):
            state["n"] += 1
            return self._cur.execute(*a, **k)

        def executemany(self, *a, **k):
            state["n"] += 1
            return self._cur.executemany(*a, **k)

        def __getattr__(self, name):
            return getattr(self._cur, name)

    @contextmanager
    def _counting():
        with _real() as cur:
            yield _CountingCursor(cur)

    _targets = {"app.storage.db": "get_cursor",
                "app.domain.invoices": "get_cursor",
                "app.domain.pos": "get_cursor",
                "app.sync.article_sync": "get_cursor",
                "app.sync.bidirectional": "get_cursor",
                "app.integration.sage.writer": "get_sqlite_cursor"}
    for mod_name, attr in _targets.items():
        monkeypatch.setattr(importlib.import_module(mod_name), attr, _counting, raising=False)
    return state

@pytest.fixture(scope="session", autouse=True)
def _mock_sage_sfec():
    """Active les mocks Sage + SFEC pour toute la session perf (0 reseau)."""
    from .mocks import sage as sage_mock
    from .mocks import sfec as sfec_mock

    sage_mock.start()
    sfec_mock.start()
    yield
    sfec_mock.stop()
    sage_mock.stop()
