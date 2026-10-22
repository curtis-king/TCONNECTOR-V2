import os
import sys
import json
import logging
import secrets
import hmac
import time
logger = logging.getLogger("t-connector.auth")

try:
    from werkzeug.security import generate_password_hash, check_password_hash
except Exception:  # pragma: no cover
    def generate_password_hash(password, method="pbkdf2:sha256", salt_length=16):
        import hashlib
        salt = os.urandom(salt_length).hex()
        return "pbkdf2:sha256:260000${}${}".format(
            salt, hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000).hex())

    def check_password_hash(phash, password):
        import hashlib
        try:
            _, _, salt, digest = phash.split("$")
            return hmac.compare_digest(
                hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260000).hex(), digest)
        except Exception:
            return False

from app.storage import db as sqlite_db
from app.config.manager import get_config, save_config

ROLES = ["admin", "responsable", "caissiere", "financiere"]

# Cache de la matrice de droits (AU-27) — TTL court, invalide a chaque ecriture.
_MATRIX_CACHE = {"ts": 0.0, "data": None}
_MATRIX_TTL = 60.0

ROLE_LABELS = {
    "admin": "Administrateur",
    "responsable": "Responsable",
    "caissiere": "Caissiere",
    "financiere": "Financiere",
}

PERMISSIONS = {
    "dashboard.voir": "Voir le tableau de bord",
    "factures.voir": "Voir factures & historique",
    "factures.creer": "Creer des factures",
    "factures.valider": "Valider des factures",
    "factures.supprimer": "Supprimer des factures",
    "sfec.certifier": "Certifier SFEC",
    "sage.sync": "Lancer la sync Sage",
    "sage.pousser": "Pousser une facture vers Sage",
    "clients.voir": "Voir les clients",
    "clients.gerer": "Gerer les clients (creer/modifier/supprimer)",
    "vendeurs.voir": "Voir les vendeurs",
    "vendeurs.gerer": "Gerer les vendeurs",
    "pos.vente": "Module caisse POS",
    "config.gerer": "Configurer le connecteur",
    "utilisateurs.gerer": "Gerer les comptes & droits",
}

ALL_PERMS = list(PERMISSIONS.keys())

DEFAULT_MATRIX = {
    "admin": list(PERMISSIONS.keys()),
    "responsable": [p for p in PERMISSIONS if p not in ("config.gerer", "utilisateurs.gerer")],
    "caissiere": ["dashboard.voir", "factures.voir", "clients.voir", "vendeurs.voir", "pos.vente"],
    "financiere": [
        "dashboard.voir", "factures.voir", "factures.creer", "factures.valider",
        "sfec.certifier", "sage.sync", "sage.pousser", "clients.voir", "vendeurs.voir",
    ],
}

def get_auth_config():
    cfg = get_config()
    auth = cfg.get("auth", {}) or {}
    dash = cfg.get("dashboard", {}) or {}
    return {
        "provider": auth.get("provider", "local"),
        "max_attempts": int(auth.get("max_attempts", 5)),
        "lock_minutes": int(auth.get("lock_minutes", 15)),
        "session_max_age_hours": int(auth.get("session_max_age_hours", dash.get("session_max_age_hours", 24))),
        "session_idle_minutes": int(auth.get("session_idle_minutes", 60)),
        "https_only": bool(auth.get("https_only", False)),
    }


def save_auth_config(values):
    cfg = get_config()
    auth = cfg.get("auth", {}) or {}
    if "provider" in values and values["provider"] not in ("local", "sage"):
        raise ValueError("Provider d'authentification invalide")
    bounds = {
        "max_attempts": (1, 20),
        "lock_minutes": (1, 1440),
        "session_max_age_hours": (1, 720),
        "session_idle_minutes": (0, 720),
    }
    for key, (low, high) in bounds.items():
        if key in values:
            try:
                number = int(values[key])
            except (TypeError, ValueError):
                raise ValueError("{} invalide".format(key))
            if number < low or number > high:
                raise ValueError("{} hors bornes ({}-{})".format(key, low, high))
            values[key] = number
    if "https_only" in values:
        values["https_only"] = bool(values["https_only"])
    for k in ("provider", "max_attempts", "lock_minutes", "session_max_age_hours", "session_idle_minutes", "https_only"):
        if k in values:
            auth[k] = values[k]
    cfg["auth"] = auth
    from app.config.manager import save_config
    save_config(cfg)
    return get_auth_config()


def _ensure_column(cur, table, column, ddl):
    """Migration douce : ajoute la colonne si absente (bases existantes)."""
    cols = [r["name"] for r in cur.execute(
        "PRAGMA table_info({})".format(table)).fetchall()]
    if column not in cols:
        cur.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, column, ddl))


def ensure_schema():
    with sqlite_db.get_cursor() as cur:
        cur.executescript("""
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
            CREATE INDEX IF NOT EXISTS idx_utilisateurs_email ON utilisateurs(email);
            CREATE INDEX IF NOT EXISTS idx_utilisateurs_email_lower ON utilisateurs(lower(email));
            CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
        """)
        # session_version : revoque les sessions apres changement de mot de
        # passe (AU-13). must_change_pwd : force le changement a la prochaine
        # connexion (AU-17 / AU-03).
        _ensure_column(cur, "utilisateurs", "session_version",
                       "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(cur, "utilisateurs", "must_change_pwd",
                       "INTEGER NOT NULL DEFAULT 0")


def migrate_admin_from_config():
    ensure_schema()
    with sqlite_db.get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM utilisateurs")
        if cur.fetchone()["c"] > 0:
            return None
        cfg = get_config().get("dashboard", {}) or {}
        email = (cfg.get("login_email") or "").strip().lower()
        password = cfg.get("login_password") or ""
        if not email or not password:
            return None
        pwd_hash = generate_password_hash(password, method="pbkdf2:sha256", salt_length=16)
        # must_change_pwd=1 : l'admin cree depuis la config DOIT changer son
        # mot de passe a la premiere connexion (attenue AU-03).
        cur.execute(
            "INSERT INTO utilisateurs (nom, prenom, email, mot_de_passe, role, must_change_pwd)"
            " VALUES (?, ?, ?, ?, ?, 1)",
            ("Administrateur", "", email, pwd_hash, "admin")
        )
        audit("admin.migre", "Compte admin migre depuis config.json (mot de passe a changer)", email=email)
        logger.info("Compte admin migre depuis la config: %s", email)
    # Le mot de passe est desormais hache en base : ne jamais conserver la
    # copie en clair dans config.json (audit DI-39).
    try:
        full_cfg = get_config()
        if (full_cfg.get("dashboard") or {}).get("login_password"):
            full_cfg.setdefault("dashboard", {})["login_password"] = ""
            save_config(full_cfg)
    except Exception as exc:
        logger.warning("Impossible de purger le mot de passe en clair de config.json: %s", exc)
    return email


def validate_password(password):
    """Politique minimale : 10 caracteres, au moins une lettre et un chiffre."""
    password = password or ""
    if len(password) < 10:
        raise ValueError("Mot de passe trop court (minimum 10 caracteres)")
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        raise ValueError("Le mot de passe doit contenir au moins une lettre et un chiffre")


def hash_password(password):
    return generate_password_hash(password, method="pbkdf2:sha256", salt_length=16)


def check_password(pwd_hash, password):
    return check_password_hash(pwd_hash, password)


def role_permissions(role):
    if role not in ROLES:
        return set()
    matrix = get_matrix()
    return set(matrix.get(role, DEFAULT_MATRIX.get(role, [])))


def has_perm(role, perm):
    perms = role_permissions(role)
    return perm in perms


def get_matrix():
    now = time.time()
    if _MATRIX_CACHE["data"] is not None and now - _MATRIX_CACHE["ts"] < _MATRIX_TTL:
        return _MATRIX_CACHE["data"]
    stored = sqlite_db.get_setting("auth_matrix", "")
    data = None
    if stored:
        try:
            parsed = json.loads(stored)
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            pass
    if data is None:
        data = {r: list(DEFAULT_MATRIX[r]) for r in ROLES}
    # Cache court (AU-27) : evite de relire le reglage SQLite a chaque
    # controle de permission ; invalide par set_matrix().
    _MATRIX_CACHE["ts"] = now
    _MATRIX_CACHE["data"] = data
    return data


def set_matrix(matrix):
    clean = {r: [p for p in perms if p in ALL_PERMS] for r, perms in matrix.items() if r in ROLES}
    sqlite_db.set_setting("auth_matrix", json.dumps(clean, ensure_ascii=False))
    _MATRIX_CACHE["data"] = None
    _MATRIX_CACHE["ts"] = 0.0
    return clean


def _user_row_to_dict(row):
    return {
        "id": row["id"],
        "nom": row["nom"] or "",
        "prenom": row["prenom"] or "",
        "email": row["email"] or "",
        "role": row["role"] or "responsable",
        "role_label": ROLE_LABELS.get(row["role"], row["role"]),
        "est_actif": bool(row["est_actif"]),
        "session_version": row["session_version"] if "session_version" in row.keys() else 0,
        "must_change_pwd": bool(row["must_change_pwd"]) if "must_change_pwd" in row.keys() else False,
        "derniere_connexion": row["derniere_connexion"] or "",
        "created_at": row["created_at"] or "",
    }


def list_users():
    ensure_schema()
    migrate_admin_from_config()
    with sqlite_db.get_cursor() as cur:
        cur.execute(
            "SELECT id, nom, prenom, email, role, est_actif, derniere_connexion, created_at "
            "FROM utilisateurs ORDER BY role, email"
        )
        return [_user_row_to_dict(r) for r in cur.fetchall()]


def get_user(user_id):
    ensure_schema()
    with sqlite_db.get_cursor() as cur:
        cur.execute(
            "SELECT id, nom, prenom, email, role, est_actif, derniere_connexion, created_at, "
            "session_version, must_change_pwd "
            "FROM utilisateurs WHERE id = ?",
            (user_id,),
        )
        row = cur.fetchone()
        return _user_row_to_dict(row) if row else None


def get_user_by_email(email):
    ensure_schema()
    email = (email or "").strip().lower()
    with sqlite_db.get_cursor() as cur:
        cur.execute(
            "SELECT id, nom, prenom, email, role, mot_de_passe, est_actif, failed_attempts, locked_until, "
            "derniere_connexion, created_at, session_version, must_change_pwd "
            "FROM utilisateurs WHERE lower(email) = ?",
            (email,),
        )
        return cur.fetchone()


def create_user(nom, prenom, email, password, role="responsable"):
    ensure_schema()
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("Adresse email invalide")
    validate_password(password)
    if role not in ROLES:
        raise ValueError("Role invalide")
    with sqlite_db.get_cursor() as cur:
        cur.execute("SELECT id FROM utilisateurs WHERE lower(email) = ?", (email,))
        if cur.fetchone():
            raise ValueError("Un compte existe deja avec cet email")
        cur.execute(
            "INSERT INTO utilisateurs (nom, prenom, email, mot_de_passe, role) VALUES (?, ?, ?, ?, ?)",
            ((nom or "").strip(), (prenom or "").strip(), email,
             hash_password(password), role)
        )
        new_id = cur.lastrowid
    audit("utilisateur.cree", "Compte cree (role {})".format(role), email=email)
    return new_id


def update_user(user_id, nom=None, prenom=None, role=None, est_actif=None,
                acting_user_id=None):
    ensure_schema()
    cur_user = get_user(user_id)
    if not cur_user:
        return False
    if acting_user_id is not None and user_id == acting_user_id:
        if role is not None and role != cur_user.get("role"):
            raise ValueError("Impossible de modifier votre propre role")
        if est_actif is not None and not est_actif:
            raise ValueError("Impossible de desactiver votre propre compte")
    if cur_user.get("role") == "admin" and (
            (role is not None and role != "admin") or est_actif is False):
        with sqlite_db.get_cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM utilisateurs "
                        "WHERE role = 'admin' AND est_actif = 1")
            if cur.fetchone()["c"] <= 1:
                raise ValueError("Impossible de retirer le dernier administrateur actif")
    fields, params = [], []
    if nom is not None:
        fields.append("nom = ?")
        params.append(str(nom).strip())
    if prenom is not None:
        fields.append("prenom = ?")
        params.append(str(prenom).strip())
    if role is not None:
        if role not in ROLES:
            raise ValueError("Role invalide")
        fields.append("role = ?")
        params.append(role)
    if est_actif is not None:
        fields.append("est_actif = ?")
        params.append(1 if est_actif else 0)
    if not fields:
        return True
    fields.append("updated_at = datetime('now')")
    params.append(user_id)
    with sqlite_db.get_cursor() as cur:
        cur.execute("UPDATE utilisateurs SET {} WHERE id = ?".format(", ".join(fields)), params)
    audit("utilisateur.modifie", "Profil mis a jour", email=cur_user["email"])
    return True


def set_password(user_id, new_password, resetter=None, must_change=False):
    ensure_schema()
    validate_password(new_password)
    cur_user = get_user(user_id)
    if not cur_user:
        raise ValueError("Compte introuvable")
    with sqlite_db.get_cursor() as cur:
        # session_version + 1 : toutes les sessions existantes de ce compte
        # sont revoquees (AU-13) — l'appelant remet a jour sa propre session.
        cur.execute(
            "UPDATE utilisateurs SET mot_de_passe = ?, failed_attempts = 0, locked_until = NULL, "
            "session_version = session_version + 1, must_change_pwd = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (hash_password(new_password), 1 if must_change else 0, user_id),
        )
    who = resetter or cur_user["email"]
    audit("utilisateur.mot_de_passe", "Mot de passe reinitialise" + ("" if resetter else " (changement personnel)"),
          email=cur_user["email"], actor=who)
    return True


def delete_user(user_id, acting_user_id=None):
    ensure_schema()
    cur_user = get_user(user_id)
    if not cur_user:
        raise ValueError("Compte introuvable")
    if acting_user_id is not None and user_id == acting_user_id:
        raise ValueError("Impossible de supprimer votre propre compte")
    with sqlite_db.get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM utilisateurs WHERE role = 'admin' AND est_actif = 1")
        admins = cur.fetchone()["c"]
        if cur_user["role"] == "admin" and admins <= 1:
            raise ValueError("Impossible de supprimer le dernier administrateur actif")
        cur.execute("DELETE FROM utilisateurs WHERE id = ?", (user_id,))
    audit("utilisateur.supprime", "Compte supprime", email=cur_user["email"])
    return True


def _sage_verify_password(email, password):
    try:
        from app.integration.sage import database
        tables = [t for t in database.list_all_tables() or [] if "utilisat" in (t or "").lower()]
        if not tables:
            return None
        columns = None
        rows = None
        for table in tables:
            try:
                with database.get_cursor() as cur:
                    cur.execute("SELECT TOP 1 * FROM [{}]".format(table))
                    columns = [d[0] for d in (cur.description or [])]
                    rows = [dict(zip(columns, r)) for r in cur.fetchall()]
                if rows:
                    break
            except Exception:
                continue
        if not rows:
            return None
        email_col = next((c for c in columns if "MAIL" in c.upper()), None)
        login_col = next((c for c in columns if c.upper() in ("US_CODE", "US_NOM", "CODE", "LOGIN", "NOM")), None)
        pwd_col = next((c for c in columns if "PASS" in c.upper() or "MDP" in c.upper()), None)
        target = None
        if email_col and not pwd_col:
            return None
        if login_col and pwd_col:
            for r in rows:
                val = str(r.get(login_col) or "").strip().lower()
                if email and val in (email.lower(), email.split("@")[0].lower()):
                    target = r
                    break
        if target is None:
            return None
        stored = str(target.get(pwd_col) or "")
        if not stored:
            return False
        if stored == password:
            return True
        import hashlib
        if hashlib.md5(password.encode()).hexdigest() == stored.lower():
            return True
        return False
    except Exception as e:
        logger.warning("Sage auth best-effort impossible: %s", e)
        return None


def _recent_failures(email, ip, minutes):
    """Nombre d'échecs récents pour ce couple email+IP.

    Le verrouillage est volontairement PAR IP : un attaquant ne peut plus
    verrouiller le compte admin d'un utilisateur légitime (DoS, audit AU-02).
    """
    if not ip:
        return 0
    ensure_schema()
    with sqlite_db.get_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM audit_log "
            "WHERE action = 'connexion.echec' AND lower(utilisateur) = lower(?) "
            "AND ip = ? AND ts >= datetime('now', ?)",
            (email, ip, "-{} minutes".format(max(1, int(minutes)))),
        )
        return int(cur.fetchone()["c"] or 0)


def authenticate(email, password, ip=""):
    ensure_schema()
    migrate_admin_from_config()
    email = (email or "").strip().lower()
    generic = "Identifiants incorrects"
    cfg = get_auth_config()
    max_attempts = max(1, int(cfg.get("max_attempts", 5) or 5))
    lock_minutes = max(1, int(cfg.get("lock_minutes", 15) or 15))

    # Throttle par email+IP : pas de verrou global du compte.
    if _recent_failures(email, ip, lock_minutes) >= max_attempts:
        audit("connexion.verrouille", "Trop de tentatives depuis cette IP",
              email=email, ip=ip)
        return None, generic

    row = get_user_by_email(email)
    if row is None:
        time.sleep(0.4)
        audit("connexion.echec", "Email inconnu", email=email, ip=ip)
        return None, generic
    if not row["est_actif"]:
        audit("connexion.refusee", "Compte desactive", email=email, ip=ip)
        return None, generic

    provider = cfg.get("provider", "local")
    pwd_ok = None
    if provider == "sage":
        pwd_ok = _sage_verify_password(email, password)
        if pwd_ok is None:
            # Repli sur le hash local : trace explicitement — le mode
            # d'authentification effectif differe de la config (AU-14).
            audit("connexion.repli_local",
                  "Provider sage indisponible/incomplet : verification du hash local",
                  email=email, ip=ip)
            logger.warning("Auth sage indisponible pour %s : repli sur le hash local", email)
    if pwd_ok is None:
        pwd_ok = check_password(row["mot_de_passe"], password)
    if pwd_ok:
        with sqlite_db.get_cursor() as cur:
            cur.execute(
                "UPDATE utilisateurs SET failed_attempts = 0, locked_until = NULL, "
                "derniere_connexion = datetime('now'), updated_at = datetime('now') WHERE id = ?",
                (row["id"],),
            )
        audit("connexion.ok", "Connexion reussie", email=email, ip=ip)
        return {"id": row["id"], "email": email, "role": row["role"],
                "nom": row["nom"] or "", "prenom": row["prenom"] or "",
                "session_version": row["session_version"] if "session_version" in row.keys() else 0,
                "must_change_pwd": bool(row["must_change_pwd"]) if "must_change_pwd" in row.keys() else False}, None

    attempts = (row["failed_attempts"] or 0) + 1
    with sqlite_db.get_cursor() as cur:
        cur.execute(
            "UPDATE utilisateurs SET failed_attempts = ?, locked_until = NULL WHERE id = ?",
            (attempts, row["id"]),
        )
    audit("connexion.echec", "Mot de passe invalide (tentative {})".format(attempts),
          email=email, ip=ip)
    # Message strictement générique : pas d'indice sur l'existence/état du compte.
    return None, generic


def audit(action, detail="", email="", actor=None, ip=""):
    try:
        if not ip:
            try:
                from flask import has_request_context, request
                if has_request_context():
                    ip = request.remote_addr or ""
            except Exception:
                pass
        ensure_schema()
        with sqlite_db.get_cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log (utilisateur, action, detail, ip) VALUES (?, ?, ?, ?)",
                (actor or email or "", action, detail[:500], ip or ""),
            )
    except Exception as e:
        logger.warning("Audit log impossible: %s", e)


def list_audit(limit=200, offset=0):
    ensure_schema()
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 200
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    with sqlite_db.get_cursor() as cur:
        cur.execute(
            "SELECT id, ts, utilisateur, action, detail, ip FROM audit_log "
            "ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [dict(r) for r in cur.fetchall()]


def count_audit():
    ensure_schema()
    with sqlite_db.get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM audit_log")
        return int(cur.fetchone()["n"])