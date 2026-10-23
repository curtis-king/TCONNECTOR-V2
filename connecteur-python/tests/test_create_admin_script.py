"""Tests du script CLI de creation / reinitialisation d'administrateur.

Les tests lancent le script dans un processus separe avec une base SQLite
temporaire. Ils ne touchent jamais a data/tconnector.db.
"""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from werkzeug.security import check_password_hash

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "create_admin.py"

EXPECTED_PERMS = {
    "dashboard.voir",
    "factures.voir",
    "factures.creer",
    "factures.valider",
    "factures.supprimer",
    "sfec.certifier",
    "sage.sync",
    "sage.pousser",
    "clients.voir",
    "clients.gerer",
    "vendeurs.voir",
    "vendeurs.gerer",
    "pos.vente",
    "config.gerer",
    "utilisateurs.gerer",
}


def _run(tmp_path, password, *extra, config=None):
    db_path = tmp_path / "data" / "tconnector.db"
    config_path = tmp_path / "config.json"
    if config is not None:
        config_path.write_text(json.dumps(config), encoding="utf-8")

    env = os.environ.copy()
    env.pop("TCONNECTOR_DB_PATH", None)
    env.pop("TCONNECTOR_CONFIG_FILE", None)
    env.pop("TCONNECTOR_ALLOW_SEED_ADMIN", None)
    env["TCONNECTOR_NEW_PASSWORD"] = password

    command = [
        sys.executable,
        str(SCRIPT),
        "--email",
        "admin@client.cg",
        "--db",
        str(db_path),
        "--config",
        str(config_path),
        "--no-backup",
    ]
    command.extend(extra)
    result = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result, db_path, config_path


def _config():
    return {
        "dashboard": {
            "auth_enabled": True,
            "login_email": "admin@example.com",
            "login_password": "CHANGE-ME",
        }
    }


def test_create_admin_and_all_permissions(tmp_path):
    result, db_path, config_path = _run(
        tmp_path,
        "AdminClient2026",
        "--create-db",
        "--nom",
        "Administrateur",
        config=_config(),
    )

    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM utilisateurs WHERE lower(email) = 'admin@client.cg'"
        ).fetchone()
        assert row is not None
        assert row["role"] == "admin"
        assert row["est_actif"] == 1
        assert row["must_change_pwd"] == 0
        assert row["failed_attempts"] == 0
        assert check_password_hash(row["mot_de_passe"], "AdminClient2026")

        matrix_row = conn.execute(
            "SELECT value FROM settings WHERE key = 'auth_matrix'"
        ).fetchone()
        assert matrix_row is not None
        matrix = json.loads(matrix_row["value"])
        assert set(matrix["admin"]) == EXPECTED_PERMS

        audit = conn.execute(
            "SELECT action FROM audit_log WHERE action = 'admin.script'"
        ).fetchone()
        assert audit is not None
    finally:
        conn.close()

    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["dashboard"]["login_password"] == ""


def test_change_me_is_rejected_before_database_creation(tmp_path):
    result, db_path, _ = _run(
        tmp_path,
        "CHANGE-ME",
        "--create-db",
        config=_config(),
    )

    assert result.returncode == 2
    assert "10 caracteres" in result.stderr
    assert "CHANGE-ME" in result.stderr
    assert not db_path.exists()


def test_reset_unlocks_and_replaces_password(tmp_path):
    first, db_path, _ = _run(
        tmp_path,
        "AdminClient2026",
        "--create-db",
        config=_config(),
    )
    assert first.returncode == 0, first.stderr

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE utilisateurs SET est_actif = 0, must_change_pwd = 1, "
            "failed_attempts = 5, locked_until = datetime('now') "
            "WHERE lower(email) = 'admin@client.cg'"
        )
        for _ in range(5):
            conn.execute(
                "INSERT INTO audit_log (utilisateur, action, detail, ip) "
                "VALUES ('admin@client.cg', 'connexion.echec', 'test', '10.0.0.1')"
            )
        conn.commit()
    finally:
        conn.close()

    result, _, _ = _run(
        tmp_path,
        "NouveauPass2026",
        "--reset",
        config=_config(),
    )
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM utilisateurs WHERE lower(email) = 'admin@client.cg'"
        ).fetchone()
        assert row["role"] == "admin"
        assert row["est_actif"] == 1
        assert row["must_change_pwd"] == 0
        assert row["failed_attempts"] == 0
        assert row["locked_until"] is None
        assert check_password_hash(row["mot_de_passe"], "NouveauPass2026")
        assert not check_password_hash(row["mot_de_passe"], "AdminClient2026")

        failures = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'connexion.echec'"
        ).fetchone()[0]
        assert failures == 0
        unlock = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'admin.deverrouillage'"
        ).fetchone()[0]
        assert unlock >= 1
    finally:
        conn.close()


def test_reset_required_for_existing_account(tmp_path):
    first, db_path, _ = _run(
        tmp_path,
        "AdminClient2026",
        "--create-db",
        config=_config(),
    )
    assert first.returncode == 0, first.stderr

    result, _, _ = _run(
        tmp_path,
        "AutrePass2026",
        config=_config(),
    )
    assert result.returncode == 1
    assert "existe deja" in result.stderr
    assert "reset" in result.stderr.lower()


def test_diagnose_does_not_create_missing_database(tmp_path):
    result, db_path, _ = _run(
        tmp_path,
        "AdminClient2026",
        "--diagnose",
        config=_config(),
    )

    assert result.returncode == 1
    assert "base SQLite absente" in result.stdout
    assert not db_path.exists()


def test_keep_config_password(tmp_path):
    result, _, config_path = _run(
        tmp_path,
        "AdminClient2026",
        "--create-db",
        "--keep-config-password",
        config=_config(),
    )

    assert result.returncode == 0, result.stderr
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["dashboard"]["login_password"] == "CHANGE-ME"


def test_backup_is_created_before_reset(tmp_path):
    first, db_path, _ = _run(
        tmp_path,
        "AdminClient2026",
        "--create-db",
        config=_config(),
    )
    assert first.returncode == 0, first.stderr

    env = os.environ.copy()
    env.pop("TCONNECTOR_DB_PATH", None)
    env.pop("TCONNECTOR_CONFIG_FILE", None)
    env["TCONNECTOR_NEW_PASSWORD"] = "NouveauPass2026"
    config_path = tmp_path / "config.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--email",
            "admin@client.cg",
            "--db",
            str(db_path),
            "--config",
            str(config_path),
            "--reset",
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    backups = list((tmp_path / "data").glob("tconnector.db.bak-*"))
    assert backups
    assert "Backup :" in result.stdout


def test_permissions_list_matches_application():
    """Le script est autonome : sa liste doit rester alignee sur l'application."""
    from app.web.auth import user_auth

    spec = importlib.util.spec_from_file_location("create_admin_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert set(module.ALL_PERMS) == set(user_auth.ALL_PERMS)
