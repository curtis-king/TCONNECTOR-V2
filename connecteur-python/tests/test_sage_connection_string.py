"""Chaîne de connexion ODBC : le port doit être intégré à SERVER.

Régression constatée le 2026-11-23 : `PORT=1444` est ignoré par MS ODBC
17/18, ce qui produisait un « Login timeout expired » alors que le serveur
était joignable avec `SERVER=tcp:hote,1444` (comme `sqlcmd`).
"""
import pytest

from app.integration.sage import database as sage_db


@pytest.mark.parametrize("server,port,expected", [
    ("tcp:127.0.0.1", 1444, "tcp:127.0.0.1,1444"),
    ("127.0.0.1", 1444, "127.0.0.1,1444"),
    ("tcp:hote.local", 1433, "tcp:hote.local,1433"),
    # Instance nommée : pas de port explicite.
    ("WIN-LA0299HF4VL\\SQLEXPRESS", 1444, "WIN-LA0299HF4VL\\SQLEXPRESS"),
    # Port déjà présent : ne pas le dupliquer.
    ("tcp:hote,1500", 1444, "tcp:hote,1500"),
    ("hote,1500", 1444, "hote,1500"),
    # Port absent ou invalide : serveur inchangé.
    ("tcp:hote", None, "tcp:hote"),
    ("tcp:hote", "abc", "tcp:hote"),
])
def test_server_clause(server, port, expected):
    assert sage_db._server_clause(server, port) == expected


def test_conn_string_integre_le_port_dans_server():
    cfg = {
        "server": "tcp:127.0.0.1",
        "port": 1444,
        "database": "BIJOU",
        "user": "sageUser",
        "password": "secret",
        "trusted_connection": False,
        "encrypt": False,
        "trust_server_certificate": True,
    }
    conn_str = sage_db._build_conn_string(cfg)
    assert "SERVER=tcp:127.0.0.1,1444" in conn_str
    # La clé PORT= est ignorée par les pilotes MS ODBC : elle ne doit plus
    # être émise du tout.
    assert "PORT=" not in conn_str
    assert "DATABASE=BIJOU" in conn_str
    assert "UID=sageUser" in conn_str
    assert "Encrypt=no" in conn_str
    assert "TrustServerCertificate=yes" in conn_str


def test_conn_string_instance_nommee_sans_port():
    cfg = {
        "server": "SRV\\SQLEXPRESS",
        "port": 1444,
        "database": "BIJOU",
        "trusted_connection": True,
    }
    conn_str = sage_db._build_conn_string(cfg)
    assert "SERVER=SRV\\SQLEXPRESS" in conn_str
    assert "PORT=" not in conn_str
    assert "Trusted_Connection=yes" in conn_str
