#!/usr/bin/env python3
"""Cree ou reinitialise un administrateur T-CONNECTOR depuis PowerShell.

Le script est volontairement autonome : il n'utilise que la bibliotheque
standard Python et le fichier SQLite de l'application. Il est donc utilisable
sur un poste client ou Python est installe, meme si les dependances de
l'application ne le sont pas.

Le hash genere est compatible avec Werkzeug (format
``pbkdf2:sha256:260000$sel$empreinte``) et avec le fallback interne de
``app/web/auth/user_auth.py``.

Usage PowerShell (recommande) :
    .\\scripts\\create_admin.ps1 -Email admin@client.cg -Reset

Usage Python direct :
    .\\.venv\\Scripts\\python.exe .\\scripts\\create_admin.py ^
        --email admin@client.cg --reset

Mot de passe :
    - si TCONNECTOR_NEW_PASSWORD est defini, il est utilise ;
    - sinon le script le demande de facon masquee ;
    - sinon --password-stdin permet une automatisation.

Diagnostic sans modification :
    .\\scripts\\create_admin.ps1 -Email admin@client.cg -Diagnose
"""
import argparse
import getpass
import hashlib
import json
import os
import secrets
import string
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Doit rester aligne sur app/web/auth/user_auth.py::ALL_PERMS.
ALL_PERMS = [
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
]

PBKDF2_ITERATIONS = 260000
DEFAULT_PASSWORD_ENV = "TCONNECTOR_NEW_PASSWORD"
ROLE = "admin"


class ScriptError(Exception):
    """Erreur fonctionnelle affichee sans traceback."""


def _base_dir():
    """Meme logique que app/core/paths.py (source ou binaire PyInstaller)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return PROJECT_ROOT


def _resolve_path(value, default):
    if not value:
        return default
    if not os.path.isabs(value):
        value = os.path.join(_base_dir(), value)
    return os.path.abspath(value)


def resolve_paths(args):
    config_path = _resolve_path(
        args.config,
        os.environ.get("TCONNECTOR_CONFIG_FILE")
        or os.path.join(_base_dir(), "config.json"),
    )
    db_path = _resolve_path(
        args.db,
        os.environ.get("TCONNECTOR_DB_PATH")
        or os.path.join(_base_dir(), "data", "tconnector.db"),
    )
    return config_path, db_path


def read_config(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print("ATTENTION: config.json illisible: {}".format(exc), file=sys.stderr)
        return {}


def clear_config_password(path):
    """Supprime le mot de passe en clair de config.json si present.

    Retourne (modifie, erreur). Une erreur de config ne doit pas annuler la
    creation du compte en base : elle est seulement signalee.
    """
    if not os.path.exists(path):
        return False, None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return False, "config.json ne contient pas un objet JSON"
        dashboard = data.get("dashboard")
        if not isinstance(dashboard, dict):
            return False, None
        if not dashboard.get("login_password"):
            return False, None
        dashboard["login_password"] = ""

        tmp_path = path + ".tmp-admin"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, path)
        return True, None
    except Exception as exc:
        try:
            if os.path.exists(path + ".tmp-admin"):
                os.unlink(path + ".tmp-admin")
        except OSError:
            pass
        return False, str(exc)


def validate_email(email):
    email = (email or "").strip().lower()
    if not email or "@" not in email or any(c.isspace() for c in email):
        raise ValueError("Adresse email invalide")
    return email


def validate_password(password):
    """Politique identique a user_auth.validate_password()."""
    password = password or ""
    if len(password) < 10:
        raise ValueError(
            "Mot de passe trop court (minimum 10 caracteres). "
            "CHANGE-ME est refuse : il ne fait que 9 caracteres."
        )
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        raise ValueError(
            "Le mot de passe doit contenir au moins une lettre et un chiffre"
        )
    return password


def hash_password(password):
    """Hash compatible Werkzeug et avec le fallback de user_auth.py."""
    salt = "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(16)
    )
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS
    ).hex()
    return "pbkdf2:sha256:{}${}${}".format(PBKDF2_ITERATIONS, salt, digest)


def read_password(args):
    if args.password_stdin:
        password = sys.stdin.read().rstrip("\r\n")
        if not password:
            raise ValueError("Aucun mot de passe recu sur stdin")
        return password

    env_name = args.password_env or DEFAULT_PASSWORD_ENV
    password = os.environ.get(env_name, "")
    if password:
        return password.rstrip("\r\n")

    if not sys.stdin.isatty():
        raise ValueError(
            "Aucun mot de passe fourni. Definir {} ou utiliser --password-stdin."
            .format(env_name)
        )

    password = getpass.getpass("Nouveau mot de passe admin: ")
    confirm = getpass.getpass("Confirmer le mot de passe: ")
    if password != confirm:
        raise ValueError("Les deux mots de passe ne correspondent pas")
    return password


def _readonly_uri(path):
    return Path(path).resolve().as_uri() + "?mode=ro"


def _table_exists(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _column_exists(conn, table, column):
    rows = conn.execute("PRAGMA table_info({})".format(table)).fetchall()
    return any(row[1] == column for row in rows)


def ensure_schema(conn):
    """Cree les tables minimales d'authentification si necessaire.

    On ne lance pas init_database() complet : le script ne doit pas modifier
    les tables metier d'une base de production.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS utilisateurs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT NOT NULL DEFAULT '',
            prenom TEXT DEFAULT '',
            email TEXT NOT NULL UNIQUE,
            mot_de_passe TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'responsable',
            est_actif INTEGER NOT NULL DEFAULT 1,
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT,
            derniere_connexion TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT DEFAULT (datetime('now')),
            utilisateur TEXT DEFAULT '',
            action TEXT NOT NULL,
            detail TEXT DEFAULT '',
            ip TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_utilisateurs_email
            ON utilisateurs(email);
        CREATE INDEX IF NOT EXISTS idx_utilisateurs_email_lower
            ON utilisateurs(lower(email));
        CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
        """
    )
    if not _column_exists(conn, "utilisateurs", "session_version"):
        conn.execute(
            "ALTER TABLE utilisateurs ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0"
        )
    if not _column_exists(conn, "utilisateurs", "must_change_pwd"):
        conn.execute(
            "ALTER TABLE utilisateurs ADD COLUMN must_change_pwd INTEGER NOT NULL DEFAULT 0"
        )


def open_database(db_path, create=False):
    if os.path.exists(db_path) and not os.path.isfile(db_path):
        raise ScriptError("Le chemin SQLite n'est pas un fichier: {}".format(db_path))
    if not os.path.exists(db_path):
        if not create:
            raise ScriptError(
                "Base SQLite introuvable: {}\n"
                "Verifier --db, ou relancer avec --create-db pour une nouvelle "
                "installation.".format(db_path)
            )
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def backup_database(db_path):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = "{}.bak-{}".format(db_path, stamp)
    suffix = 1
    while os.path.exists(backup_path):
        backup_path = "{}.bak-{}-{}".format(db_path, stamp, suffix)
        suffix += 1

    source = sqlite3.connect(db_path, timeout=30)
    destination = sqlite3.connect(backup_path)
    try:
        source.execute("PRAGMA busy_timeout = 10000")
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return backup_path


def find_user(conn, email):
    if not _table_exists(conn, "utilisateurs"):
        return None
    return conn.execute(
        "SELECT id, email FROM utilisateurs WHERE lower(email) = ?",
        (email,),
    ).fetchone()


def get_matrix(conn):
    if not _table_exists(conn, "settings"):
        return {}
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'auth_matrix'"
    ).fetchone()
    if not row or not row["value"]:
        return {}
    try:
        matrix = json.loads(row["value"])
    except Exception:
        return {}
    return matrix if isinstance(matrix, dict) else {}


def admin_permissions(conn):
    matrix = get_matrix(conn)
    # Comme user_auth.role_permissions(), une cle "admin" absente signifie
    # "matrice par defaut", donc toutes les permissions.
    if not matrix or "admin" not in matrix:
        return set(ALL_PERMS)
    return set(matrix.get("admin", []))


def apply_admin(
    conn,
    email,
    password,
    nom=None,
    prenom=None,
    reset=False,
    must_change=False,
    unlock=True,
    ensure_matrix=True,
    lock_minutes=15,
):
    existing = find_user(conn, email)
    if existing and not reset:
        raise ScriptError(
            "Un compte existe deja avec cet email: {}\n"
            "Relancer avec --reset pour reinitialiser son mot de passe et "
            "ses droits.".format(email)
        )

    ensure_schema(conn)
    password_hash = hash_password(password)

    with conn:
        # Re-verification dans la transaction pour eviter une course avec un
        # autre processus qui creerait le meme email.
        existing = find_user(conn, email)
        if existing and not reset:
            raise ScriptError(
                "Un compte existe deja avec cet email: {}. Utiliser --reset."
                .format(email)
            )
        action = "reinitialise" if existing else "cree"

        if existing:
            fields = [
                "mot_de_passe = ?",
                "role = ?",
                "est_actif = 1",
                "failed_attempts = 0",
                "locked_until = NULL",
                "session_version = session_version + 1",
                "must_change_pwd = ?",
                "updated_at = datetime('now')",
            ]
            params = [password_hash, ROLE, 1 if must_change else 0]
            if nom is not None:
                fields.insert(0, "nom = ?")
                params.insert(0, nom.strip())
            if prenom is not None:
                insert_at = 1 if nom is not None else 0
                fields.insert(insert_at, "prenom = ?")
                params.insert(insert_at, prenom.strip())
            params.append(existing["id"])
            conn.execute(
                "UPDATE utilisateurs SET {} WHERE id = ?".format(", ".join(fields)),
                params,
            )
            user_id = existing["id"]
        else:
            cursor = conn.execute(
                "INSERT INTO utilisateurs "
                "(nom, prenom, email, mot_de_passe, role, est_actif, "
                " failed_attempts, locked_until, session_version, must_change_pwd) "
                "VALUES (?, ?, ?, ?, ?, 1, 0, NULL, 0, ?)",
                (
                    (nom or "Administrateur").strip(),
                    (prenom or "").strip(),
                    email,
                    password_hash,
                    ROLE,
                    1 if must_change else 0,
                ),
            )
            user_id = cursor.lastrowid

        unlocked = 0
        if unlock:
            cursor = conn.execute(
                "DELETE FROM audit_log "
                "WHERE action = 'connexion.echec' "
                "  AND lower(utilisateur) = lower(?) "
                "  AND ts >= datetime('now', ?)",
                (email, "-{} minutes".format(max(1, int(lock_minutes)))),
            )
            unlocked = max(0, cursor.rowcount or 0)

        matrix_changed = False
        matrix = get_matrix(conn)
        wanted = set(ALL_PERMS)
        current = set(matrix.get("admin", []))
        if ensure_matrix and current != wanted:
            matrix["admin"] = list(ALL_PERMS)
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) "
                "VALUES ('auth_matrix', ?, datetime('now'))",
                (json.dumps(matrix, ensure_ascii=False),),
            )
            matrix_changed = True
        elif ensure_matrix:
            # Le reglage existe deja et contient exactement les droits.
            matrix_changed = False

        detail = "Compte admin {} par script CLI".format(action)
        conn.execute(
            "INSERT INTO audit_log (utilisateur, action, detail, ip) "
            "VALUES (?, 'admin.script', ?, '')",
            (email, detail),
        )
        if unlocked:
            conn.execute(
                "INSERT INTO audit_log (utilisateur, action, detail, ip) "
                "VALUES (?, 'admin.deverrouillage', ?, '')",
                (
                    email,
                    "{} echec(s) recent(s) purge(s) par script CLI"
                    .format(unlocked),
                ),
            )

    return {
        "user_id": user_id,
        "action": action,
        "unlocked": unlocked,
        "matrix_changed": matrix_changed,
        "must_change": bool(must_change),
        "permissions": len(admin_permissions(conn)),
    }


def print_diagnosis(config_path, db_path, email):
    config = read_config(config_path)
    auth = config.get("auth", {}) or {}
    dashboard = config.get("dashboard", {}) or {}
    provider = auth.get("provider", "local")
    auth_enabled = bool(dashboard.get("auth_enabled", True))
    max_attempts = int(auth.get("max_attempts", 5) or 5)
    lock_minutes = int(auth.get("lock_minutes", 15) or 15)

    print("Config : {}".format(config_path))
    print("DB     : {}".format(db_path))
    print("Provider auth: {}".format(provider))
    print("Auth active  : {}".format("oui" if auth_enabled else "non"))
    print("Seuil echecs : {} / {} minutes".format(max_attempts, lock_minutes))
    print(
        "Config login_password: {}".format(
            "present (non utilise si le compte existe deja)"
            if dashboard.get("login_password")
            else "vide"
        )
    )
    if os.environ.get("TCONNECTOR_ALLOW_SEED_ADMIN") == "1":
        print("TCONNECTOR_ALLOW_SEED_ADMIN=1 (bootstrap automatique autorise)")

    if not os.path.isfile(db_path):
        print("\nRESULTAT: base SQLite absente.")
        return 1

    try:
        conn = sqlite3.connect(_readonly_uri(db_path), uri=True)
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        print("\nRESULTAT: impossible d'ouvrir la base: {}".format(exc))
        return 1

    try:
        if not _table_exists(conn, "utilisateurs"):
            print("\nRESULTAT: table utilisateurs absente (base non initialisee).")
            return 1

        row = conn.execute(
            "SELECT * FROM utilisateurs WHERE lower(email) = ?",
            (email,),
        ).fetchone()
        if not row:
            print("\nCompte: ABSENT ({})".format(email))
            print(
                "RESULTAT: aucun compte avec cet email. Creer le compte avec "
                "--reset (ou sans --reset si la base est vide)."
            )
            return 1

        columns = set(row.keys())
        must_change = bool(row["must_change_pwd"]) if "must_change_pwd" in columns else False
        print("\nCompte: {} (id={})".format(row["email"], row["id"]))
        print("Role  : {}".format(row["role"]))
        print("Actif : {}".format("oui" if row["est_actif"] else "non"))
        print(
            "Changement mot de passe requis: {}".format(
                "oui" if must_change else "non"
            )
        )
        print("Echecs consecutifs: {}".format(row["failed_attempts"] or 0))
        print("locked_until: {}".format(row["locked_until"] or "aucun"))
        password_hash = row["mot_de_passe"] or ""
        print("Schema hash: {}".format(password_hash.split("$", 1)[0]))

        recent = 0
        if _table_exists(conn, "audit_log"):
            recent = conn.execute(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE action = 'connexion.echec' "
                "  AND lower(utilisateur) = lower(?) "
                "  AND ts >= datetime('now', ?)",
                (email, "-{} minutes".format(lock_minutes)),
            ).fetchone()[0]
        print("Echecs recents (toutes IP): {}".format(recent))

        blocked = False
        if not row["est_actif"]:
            print("PROBLEME: compte desactive.")
            blocked = True
        if must_change:
            print(
                "PROBLEME: changement de mot de passe force. La connexion "
                "peut reussir puis rediriger vers /compte/mot-de-passe."
            )
            blocked = True
        if recent >= max_attempts:
            print(
                "PROBLEME: throttle actif. Relancer avec --reset (purge les "
                "echecs recents)."
            )
            blocked = True
        if provider == "sage":
            print(
                "ATTENTION: auth.provider=sage. Le mot de passe local peut "
                "etre ignore si Sage repond."
            )
        if not auth_enabled:
            print("ATTENTION: dashboard.auth_enabled=false.")

        if blocked:
            print("\nRESULTAT: blocage identifie. Utiliser --reset.")
        else:
            print(
                "\nRESULTAT: aucun blocage evident. Si le mot de passe est "
                "refuse, il a probablement ete modifie depuis la creation : "
                "utiliser --reset."
            )
        return 0
    finally:
        conn.close()


def build_parser():
    parser = argparse.ArgumentParser(
        description="Cree ou reinitialise un administrateur T-CONNECTOR.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--email", required=True, help="Email du compte admin")
    parser.add_argument("--nom", default=None, help="Nom affiche")
    parser.add_argument("--prenom", default=None, help="Prenom affiche")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reinitialise le compte s'il existe deja",
    )
    parser.add_argument(
        "--unlock",
        action="store_true",
        default=True,
        help="Purge les echecs recents (actif par defaut)",
    )
    parser.add_argument(
        "--no-unlock",
        action="store_false",
        dest="unlock",
        help="Ne purge pas les echecs recents",
    )
    parser.add_argument(
        "--must-change",
        action="store_true",
        help="Force le changement de mot de passe a la premiere connexion",
    )
    parser.add_argument(
        "--ensure-matrix",
        action="store_true",
        default=True,
        help="Remet la matrice admin a toutes les permissions (defaut)",
    )
    parser.add_argument(
        "--no-matrix",
        action="store_false",
        dest="ensure_matrix",
        help="Ne modifie pas la matrice des droits",
    )
    parser.add_argument(
        "--clear-config-password",
        action="store_true",
        default=True,
        help="Vide dashboard.login_password dans config.json (defaut)",
    )
    parser.add_argument(
        "--keep-config-password",
        action="store_false",
        dest="clear_config_password",
        help="Conserve dashboard.login_password dans config.json",
    )
    parser.add_argument(
        "--password-env",
        default=DEFAULT_PASSWORD_ENV,
        help="Variable d'environnement contenant le mot de passe",
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Lit le mot de passe sur stdin",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Diagnostic seulement, aucune modification",
    )
    parser.add_argument("--config", default=None, help="Chemin de config.json")
    parser.add_argument("--db", default=None, help="Chemin de tconnector.db")
    parser.add_argument(
        "--create-db",
        action="store_true",
        help="Cree la base SQLite si elle n'existe pas",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        default=True,
        help="Sauvegarde la base avant modification (defaut)",
    )
    parser.add_argument(
        "--no-backup",
        action="store_false",
        dest="backup",
        help="Ne sauvegarde pas la base",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    config_path, db_path = resolve_paths(args)

    try:
        email = validate_email(args.email)
    except ValueError as exc:
        print("ERREUR: {}".format(exc), file=sys.stderr)
        return 2

    if args.diagnose:
        return print_diagnosis(config_path, db_path, email)

    try:
        password = read_password(args)
        validate_password(password)
    except ValueError as exc:
        print("ERREUR: {}".format(exc), file=sys.stderr)
        return 2

    config = read_config(config_path)
    auth = config.get("auth", {}) or {}
    dashboard = config.get("dashboard", {}) or {}
    provider = auth.get("provider", "local")
    lock_minutes = int(auth.get("lock_minutes", 15) or 15)

    print("Config : {}".format(config_path))
    print("DB     : {}".format(db_path))

    db_existed = os.path.isfile(db_path)
    conn = None
    try:
        conn = open_database(db_path, create=args.create_db)

        # Verification avant sauvegarde : on ne cree pas un backup inutile
        # quand l'utilisateur a simplement oublie --reset.
        existing = find_user(conn, email)
        if existing and not args.reset:
            raise ScriptError(
                "Un compte existe deja avec cet email: {}\n"
                "Relancer avec --reset pour reinitialiser son mot de passe et "
                "ses droits.".format(email)
            )

        backup_path = None
        if db_existed and args.backup:
            backup_path = backup_database(db_path)
            print("Backup : {}".format(backup_path))

        result = apply_admin(
            conn,
            email=email,
            password=password,
            nom=args.nom,
            prenom=args.prenom,
            reset=args.reset,
            must_change=args.must_change,
            unlock=args.unlock,
            ensure_matrix=args.ensure_matrix,
            lock_minutes=lock_minutes,
        )
    except ScriptError as exc:
        print("ERREUR: {}".format(exc), file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError) as exc:
        print("ERREUR base de donnees: {}".format(exc), file=sys.stderr)
        print(
            "Si le service T-CONNECTOR tourne, l'arreter puis relancer le script.",
            file=sys.stderr,
        )
        return 1
    finally:
        if conn is not None:
            conn.close()

    config_cleared = False
    config_error = None
    if args.clear_config_password:
        config_cleared, config_error = clear_config_password(config_path)

    print("Compte : {} (id={})".format(email, result["user_id"]))
    print("Action : {}".format(result["action"]))
    print("Role   : {} ({} permissions)".format(ROLE, result["permissions"]))
    print("Actif  : oui")
    print(
        "Changement de mot de passe requis : {}".format(
            "oui" if result["must_change"] else "non"
        )
    )
    print("Echecs recents purges : {}".format(result["unlocked"]))
    if not args.ensure_matrix:
        print(
            "Matrice admin : NON modifiee ({} permissions).".format(
                result["permissions"]
            )
        )
        if result["permissions"] < len(ALL_PERMS):
            print(
                "ATTENTION: le role admin n'a pas toutes les permissions. "
                "Relancer sans --no-matrix pour les remettre.",
                file=sys.stderr,
            )
    elif result["matrix_changed"]:
        print(
            "Matrice admin : remise a toutes les permissions "
            "(concerne tous les comptes admin)."
        )
    else:
        print("Matrice admin : deja complete.")
    if config_cleared:
        print("config.json : dashboard.login_password vide.")
    elif config_error:
        print(
            "ATTENTION config.json non modifie: {}".format(config_error),
            file=sys.stderr,
        )
    elif not os.path.exists(config_path):
        print("ATTENTION: config.json introuvable, non modifie.", file=sys.stderr)

    if provider == "sage":
        print(
            "ATTENTION: auth.provider=sage. Le mot de passe local peut etre "
            "ignore si Sage repond; le repli local s'applique seulement si "
            "Sage est indisponible.",
            file=sys.stderr,
        )
    if not bool(dashboard.get("auth_enabled", True)):
        print(
            "ATTENTION: dashboard.auth_enabled=false, le compte ne sera pas "
            "demande a la connexion.",
            file=sys.stderr,
        )

    print(
        "Si le service T-CONNECTOR tourne, le redemarrer pour recharger la "
        "matrice des droits (cache 60 s)."
    )
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
