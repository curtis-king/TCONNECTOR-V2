#!/usr/bin/env python3
"""Génère les snapshots HTTP de TOUTES les routes GET de l'app Flask.

Pour chaque route GET :
  - sans paramètre obligatoire  → appelée telle quelle ;
  - avec paramètres             → appelée UNIQUEMENT si des données réelles
                                  existent dans la base SQLite (sinon skippée).

Chaque réponse est sauvegardée dans tests/snapshots/<id>.json :
    méthode, path, status_code, content_type, sha256 du corps, corps complet
    (texte ou base64 si binaire).

Les routes non snapshotées sont listées dans tests/snapshots/_skipped.json
avec la raison.

USAGE :
    .venv/bin/python tests/snapshot_routes.py
"""
import base64
import hashlib
import json
import logging
import os
import re
import sys

logging.disable(logging.CRITICAL)

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOTS_DIR = os.path.join(TESTS_DIR, "snapshots")
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ── Données réelles pour les routes à paramètres ─────────────────────────
# Mapping : (converter, argument) -> (table, colonne) : recherche d'un id réel.
PARAM_SOURCES = {
    ("int", "invoice_id"): ("invoices", "id"),
    ("int", "ticket_id"): ("pos_tickets", "id"),
    ("int", "vendeur_id"): ("vendeurs", "id"),
    ("int", "contact_id"): ("contacts", "id"),
    ("int", "user_id"): ("utilisateurs", "id"),
    # invoice_id de /certified/<invoice_id>/print est une chaîne (numéro SFEC)
    ("default", "invoice_id"): ("invoices", "id"),
}


def _find_param_value(converter, arg_name):
    """Retourne une valeur réelle pour un paramètre de route, ou None."""
    from app.storage.db import get_cursor

    source = PARAM_SOURCES.get((converter, arg_name))
    if source is None:
        return None
    table, column = source
    try:
        with get_cursor() as cur:
            row = cur.execute(
                f"SELECT {column} FROM {table} LIMIT 1"  # noqa: S608 — noms internes
            ).fetchone()
            return row[0] if row else None
    except Exception:
        return None


# Cas particulier : la route statique de Flask — on snapshot un vrai fichier.
STATIC_PATHS = {"/static/<path:filename>": "/static/css/app.css"}


def _build_targets(app):
    """Retourne (targets, skipped) pour toutes les routes GET.

    targets : list of (rule, path_effectif)
    skipped : list of {path, reason}
    """
    targets, skipped = [], []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: str(r.rule)):
        methods = rule.methods - {"HEAD", "OPTIONS"}
        path = str(rule.rule)
        if "GET" not in methods:
            continue
        if path in STATIC_PATHS:
            targets.append((rule, STATIC_PATHS[path]))
            continue
        if not rule.arguments:
            targets.append((rule, path))
            continue
        # Route à paramètres : chercher des données réelles en base
        values = {}
        missing = []
        for arg_name in rule.arguments:
            conv = "default"
            for m in re.finditer(r"<(?:(\w+):)?(\w+)>", path):
                if m.group(2) == arg_name and m.group(1):
                    conv = m.group(1)
            value = _find_param_value(conv, arg_name)
            if value is None:
                missing.append(arg_name)
            else:
                values[arg_name] = value
        if missing:
            skipped.append({
                "path": path,
                "reason": "aucune donnée en base SQLite pour le(s) "
                          "paramètre(s) " + ", ".join(missing),
            })
            continue
        eff_path = path
        for arg_name, value in values.items():
            eff_path = re.sub(
                r"<(?:(\w+):)?" + re.escape(arg_name) + r">",
                str(value),
                eff_path,
            )
        targets.append((rule, eff_path))
    return targets, skipped


def _snapshot_name(index, rule, path):
    """Nom de fichier unique et lisible pour un snapshot."""
    safe = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_") or "root"
    methods = "-".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
    return f"{index:03d}_{methods}_{safe[:80]}.json"


def _body_to_json(resp):
    """Corps de réponse → dict {encoding, body, sha256}."""
    raw = resp.get_data()
    sha = hashlib.sha256(raw).hexdigest()
    content_type = resp.content_type or ""
    if content_type.startswith(("text/", "application/json")) or (
        content_type.startswith("application/") and b"\x00" not in raw[:200]
    ):
        try:
            return {"encoding": "text", "body": raw.decode("utf-8"), "sha256": sha}
        except UnicodeDecodeError:
            pass
    return {"encoding": "base64", "body": base64.b64encode(raw).decode("ascii"),
            "sha256": sha}


def _reset_audit_log():
    """Vide le journal d'audit (données de test) pour la déterminisme.

    Chaque exécution (snapshot ou rejeu) réalise un login qui écrit une
    entrée `connexion.ok`. Sans remise à zéro, les corps de /api/audit
    différeraient à chaque run pour des raisons sans rapport avec le
    refactoring. La base SQLite n'est créée/initialisée QUE par ces outils
    de test (données de développement, aucune donnée réelle).
    """
    from app.storage.db import get_cursor

    try:
        with get_cursor() as cur:
            cur.execute("DELETE FROM audit_log")
    except Exception:
        pass


def generate_snapshots():
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)

    # Purge des snapshots précédents (le manifest est régénéré intégralement)
    for f in os.listdir(SNAPSHOTS_DIR):
        if f.endswith(".json"):
            os.remove(os.path.join(SNAPSHOTS_DIR, f))

    # Init SQLite identique à main.py + login admin
    from tests.conftest import _init_sqlite, _login_client  # réutilisation

    _init_sqlite()
    _reset_audit_log()
    from app.config.manager import get_config
    from app.web.app import create_app

    app = create_app()

    # Comportement production (voir conftest.app_instance) : les exceptions
    # deviennent des réponses 500 réelles, snapshotables.
    dash = get_config().get("dashboard", {})
    client = _login_client(
        app.test_client(),
        dash.get("login_email", "admin@example.com"),
        dash.get("login_password", "CHANGE-ME"),
    )

    manifest = []
    index = 0
    targets, skipped = _build_targets(app)
    for rule, path in targets:
        resp = client.get(path)
        index += 1
        name = _snapshot_name(index, rule, path)
        payload = {
            "file": name,
            "endpoint": rule.endpoint,
            "methods": sorted(rule.methods - {"HEAD", "OPTIONS"}),
            "path": path,
            "status_code": resp.status_code,
            "content_type": resp.content_type,
            **_body_to_json(resp),
        }
        with open(os.path.join(SNAPSHOTS_DIR, name), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        manifest.append({
            "file": name,
            "path": path,
            "endpoint": rule.endpoint,
            "status_code": resp.status_code,
        })

    with open(os.path.join(SNAPSHOTS_DIR, "_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(SNAPSHOTS_DIR, "_skipped.json"), "w", encoding="utf-8") as fh:
        json.dump(skipped, fh, ensure_ascii=False, indent=2)

    print(f"✅ {len(manifest)} snapshots générés dans {SNAPSHOTS_DIR}")
    print(f"⏭  {len(skipped)} routes skippées (voir _skipped.json)")
    return len(manifest), len(skipped)


if __name__ == "__main__":
    generate_snapshots()
