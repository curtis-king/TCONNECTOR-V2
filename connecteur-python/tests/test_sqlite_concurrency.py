"""Tests du correctif 03 : connexions SQLite thread-local.

Regles verifiees :
  - 20 threads peuvent ecrire/lire en parallele sans `SystemError` ni
    `cannot commit - no transaction is active` ;
  - un contexte en lecture seule n'emet aucun commit ;
  - chaque thread obtient sa propre connexion (pas de partage).
"""
import threading

import pytest

from app.storage import db


@pytest.fixture(autouse=True)
def _clean_connections():
    yield
    db.close_connection()
    db.close_all_connections()


def _worker(n, errors):
    try:
        for i in range(100):
            with db.get_cursor() as cur:
                cur.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("test_sqlite_%d_%d" % (n, i), str(i)),
                )
            with db.get_cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM settings").fetchone()
    except Exception as e:  # noqa: BLE001
        errors.append(e)


def test_concurrence_sans_systemerror():
    db.init_database()
    errors = []
    threads = [
        threading.Thread(target=_worker, args=(i, errors), name="sqlite-test-%d" % i)
        for i in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], "Erreurs SQLite concurrentes: {!r}".format(errors)


class _FakeCursor:
    def execute(self, *args, **kwargs):
        return self

    def close(self):
        pass


class _FakeConn:
    def __init__(self, in_transaction=False):
        self.in_transaction = in_transaction
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _FakeCursor()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_get_cursor_lecture_seule_ne_commit_pas(monkeypatch):
    fake = _FakeConn(in_transaction=False)
    monkeypatch.setattr(db, "get_connection", lambda: fake)

    with db.get_cursor() as cur:
        cur.execute("SELECT 1")

    assert fake.commits == 0
    assert fake.rollbacks == 0


def test_get_cursor_ecriture_commit_une_fois(monkeypatch):
    fake = _FakeConn(in_transaction=True)
    monkeypatch.setattr(db, "get_connection", lambda: fake)

    with db.get_cursor():
        pass

    assert fake.commits == 1
    assert fake.rollbacks == 0


def test_connexion_thread_local():
    conns = {}
    barrier = threading.Barrier(2)

    def worker(name):
        conns[name] = db.get_connection()
        barrier.wait()
        db.close_connection()

    threads = [
        threading.Thread(target=worker, args=(name,), name="sqlite-local-" + name)
        for name in ("a", "b")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert conns["a"] is not conns["b"]
