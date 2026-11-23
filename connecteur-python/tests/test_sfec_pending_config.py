"""Paramètres de reprise SFEC : défauts, surcharge, valeurs invalides."""
import pytest

from app.integration.sfec import pending_writes
from app.sync import engine


def test_defauts_reprise_sfec():
    assert pending_writes.flush_batch() == 20
    assert pending_writes.pending_log_every() == 10
    assert engine._flush_interval_s() == 30
    assert engine._sfec_lookup_ttl_s() == 300


def test_surcharge_config(monkeypatch):
    cfg = {"sfec": {
        "pending_flush_batch": 5,
        "pending_log_every": 3,
        "pending_flush_interval_s": 10,
        "lookup_ttl_s": 60,
    }}
    monkeypatch.setattr(pending_writes, "get_config", lambda: cfg)
    monkeypatch.setattr(engine, "get_config", lambda: cfg)

    assert pending_writes.flush_batch() == 5
    assert pending_writes.pending_log_every() == 3
    assert engine._flush_interval_s() == 10
    assert engine._sfec_lookup_ttl_s() == 60


@pytest.mark.parametrize("valeur", ["abc", None, "", 0, -5])
def test_valeur_invalide_retombe_sur_defaut(monkeypatch, valeur):
    cfg = {"sfec": {"pending_flush_batch": valeur, "pending_log_every": valeur}}
    monkeypatch.setattr(pending_writes, "get_config", lambda: cfg)

    # 0 / négatif sont bornés au minimum (1), pas rejetés.
    assert 1 <= pending_writes.flush_batch() <= 200
    assert 1 <= pending_writes.pending_log_every() <= 1000


def test_valeur_invalide_texte_retombe_sur_defaut(monkeypatch):
    cfg = {"sfec": {"pending_flush_batch": "abc", "pending_log_every": None}}
    monkeypatch.setattr(pending_writes, "get_config", lambda: cfg)

    assert pending_writes.flush_batch() == 20
    assert pending_writes.pending_log_every() == 10


def test_bornes_config(monkeypatch):
    cfg = {"sfec": {"pending_flush_batch": 9999, "pending_flush_interval_s": 99999}}
    monkeypatch.setattr(pending_writes, "get_config", lambda: cfg)
    monkeypatch.setattr(engine, "get_config", lambda: cfg)

    assert pending_writes.flush_batch() == 200
    assert engine._flush_interval_s() == 3600
