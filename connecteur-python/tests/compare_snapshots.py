#!/usr/bin/env python3
"""Rejoue les requêtes des snapshots et compare statut + corps.

Outil de non-régression de la refactorisation : après chaque étape, exécuter
    .venv/bin/python tests/compare_snapshots.py
Un rapport OK / DIFF est affiché et le script sort avec le code 0 (tout OK)
ou 1 (au moins une DIFF ou une erreur).

USAGE :
    .venv/bin/python tests/compare_snapshots.py [--verbose]
"""
import argparse
import base64
import hashlib
import json
import logging
import os
import sys

logging.disable(logging.CRITICAL)

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOTS_DIR = os.path.join(TESTS_DIR, "snapshots")
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _body_bytes(snapshot):
    if snapshot["encoding"] == "base64":
        return base64.b64decode(snapshot["body"])
    return snapshot["body"].encode("utf-8")


def _normalize(text):
    """Neutralise les valeurs volatiles connues (jetons CSRF, timestamps).

    Le HTML actuel injecte des valeurs variables d'une exécution à l'autre :
      - le jeton CSRF de session (`secrets.token_urlsafe(32)` = 43 caractères
        URL-safe) dans les attributs value="…", les littéraux JS et le JSON
        de /api/csrf ;
      - les horodatages ISO (avec microsecondes) de /api/metrics.
    Sans normalisation, la comparaison différerait sans raison liée au
    refactoring. Les empreintes sha256 (64 caractères hexadécimaux) et les
    autres contenus de 43 caractères hors contexte CSRF ne sont PAS touchés.
    """
    import json as _json
    import re

    # Cas particulier : /api/audit — le journal grandit à CHAQUE login (chaque
    # exécution du comparateur en ajoute une entrée). On canonicalise : on
    # retire les champs volatils (id auto-incrémenté, ts, ip) et on trie les
    # entrées, pour comparer le CONTENU sémantique du journal, pas son
    # historique d'exécution.
    try:
        data = _json.loads(text)
    except Exception:
        data = None
    if isinstance(data, dict) and isinstance(data.get("entries"), list):
        entries = []
        for e in data["entries"]:
            if isinstance(e, dict):
                e = {k: v for k, v in e.items() if k not in ("id", "ts", "ip")}
            entries.append(e)
        data["entries"] = sorted(
            entries,
            key=lambda e: _json.dumps(e, sort_keys=True, ensure_ascii=False),
        )
        return _json.dumps(data, sort_keys=True, ensure_ascii=False)

    # Jeton CSRF de session (secrets.token_urlsafe(32) = 43 caractères
    # URL-safe). Contextes d'injection observés dans le HTML actuel :
    #   - <script>window.CSRF_TOKEN="…";</script>      (toutes les pages)
    #   - <input type="hidden" name="_csrf" value="…"> (formulaires)
    #   - {"token":"…"}                                 (réponse de /api/csrf)
    csrf_tok = r"[A-Za-z0-9_\-]{43}"
    text = re.sub(
        r"(CSRF_TOKEN\s*=\s*)([\"'])" + csrf_tok + r"([\"'])",
        r"\1\2<CSRF>\3", text)
    text = re.sub(
        r"(value=\")" + csrf_tok + r"(\")", r"\1<CSRF>\2", text)
    text = re.sub(
        r"(\"token\"\s*:\s*[\"'])" + csrf_tok + r"([\"'])",
        r"\1<CSRF>\2", text)
    # Horodatages ISO complets, avec fraction de seconde / fuseau optionnels
    text = re.sub(
        r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?",
        "<TIMESTAMP>",
        text,
    )
    # Timestamps epoch à 10 chiffres
    text = re.sub(r"\b1[5-9]\d{8}\b", "<EPOCH>", text)
    return text


def compare_snapshots(verbose=False):
    manifest_path = os.path.join(SNAPSHOTS_DIR, "_manifest.json")
    if not os.path.exists(manifest_path):
        print("❌ _manifest.json introuvable — lancez d'abord "
              "tests/snapshot_routes.py")
        return 1

    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)

    from tests.conftest import _init_sqlite, _login_client

    from tests.snapshot_routes import _reset_audit_log  # même règle de
    # déterminisme que lors de la génération

    _init_sqlite()
    _reset_audit_log()
    from app.config.manager import get_config
    from app.web.dashboard import app

    # Comportement production (voir conftest.app_instance).
    dash = get_config().get("dashboard", {})
    client = _login_client(
        app.test_client(),
        dash.get("login_email", "admin@example.com"),
        dash.get("login_password", "CHANGE-ME"),
    )

    results = []
    for entry in manifest:
        snap_file = os.path.join(SNAPSHOTS_DIR, entry["file"])
        with open(snap_file, encoding="utf-8") as fh:
            snap = json.load(fh)

        try:
            resp = client.get(snap["path"])
        except Exception as exc:  # noqa: BLE001
            results.append((entry["path"], "ERROR", f"exception: {exc}"))
            continue

        status_ok = resp.status_code == snap["status_code"]
        raw = resp.get_data()
        new_sha = hashlib.sha256(raw).hexdigest()
        body_ok = new_sha == snap["sha256"]

        # Comparaison normalisée en secours (diagnostic uniquement)
        if not body_ok and snap["encoding"] == "text":
            try:
                old_norm = _normalize(snap["body"])
                new_norm = _normalize(raw.decode("utf-8"))
                if old_norm == new_norm:
                    body_ok = True
                    results.append((entry["path"], "OK(norm)",
                                    "identique après normalisation des "
                                    "valeurs volatiles"))
                    continue
            except UnicodeDecodeError:
                pass

        if status_ok and body_ok:
            results.append((entry["path"], "OK", ""))
        else:
            details = []
            if not status_ok:
                details.append(
                    f"statut {snap['status_code']} → {resp.status_code}")
            if not body_ok:
                details.append("corps différent "
                               f"(sha {snap['sha256'][:12]}… → "
                               f"{new_sha[:12]}…)")
            results.append((entry["path"], "DIFF", "; ".join(details)))

    n_ok = sum(1 for _, s, _ in results if s.startswith("OK"))
    n_diff = len(results) - n_ok

    print(f"\n{'='*70}")
    print(f"RAPPORT : {n_ok}/{len(results)} OK, {n_diff} DIFF/ERROR")
    print("=" * 70)
    for path, status, note in results:
        if status != "OK" or verbose:
            suffix = f" — {note}" if note else ""
            print(f"[{status:9s}] {path}{suffix}")

    return 1 if n_diff else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="afficher aussi les routes OK")
    args = parser.parse_args()
    sys.exit(compare_snapshots(verbose=args.verbose))
