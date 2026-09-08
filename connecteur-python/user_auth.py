import os
import sys
import json
import logging
import functools
import secrets
import hmac
import time
from datetime import datetime, timedelta

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

import sqlite_db
from config_manager import get_config

ROLES = ["admin", "responsable", "caissiere", "financiere"]

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

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _now():
    return datetime.now()


def _ts(dt):
    return dt.strftime(_TS_FORMAT)


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
    for k in ("provider", "max_attempts", "lock_minutes", "session_max_age_hours", "session_idle_minutes", "https_only"):
        if k in values:
            auth[k] = values[k]
    cfg["auth"] = auth
    from config_manager import save_config
    save_config(cfg)
    return get_auth_config()


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
            CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
        """)


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
        cur.execute(
            "INSERT INTO utilisateurs (nom, prenom, email, mot_de_passe, role) VALUES (?, ?, ?, ?, ?)",
            ("Administrateur", "", email, pwd_hash, "admin")
        )
        audit("admin.migre", "Compte admin migre depuis config.json (mot de passe a changer)", email=email)
        logger.info("Compte admin migre depuis la config: %s", email)
        return email


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
    stored = sqlite_db.get_setting("auth_matrix", "")
    if stored:
        try:
            data = json.loads(stored)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {r: list(DEFAULT_MATRIX[r]) for r in ROLES}


def set_matrix(matrix):
    clean = {r: [p for p in perms if p in ALL_PERMS] for r, perms in matrix.items() if r in ROLES}
    sqlite_db.set_setting("auth_matrix", json.dumps(clean, ensure_ascii=False))
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
            "SELECT id, nom, prenom, email, role, est_actif, derniere_connexion, created_at "
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
            "derniere_connexion, created_at FROM utilisateurs WHERE lower(email) = ?",
            (email,),
        )
        return cur.fetchone()


def create_user(nom, prenom, email, password, role="responsable"):
    ensure_schema()
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("Adresse email invalide")
    if len(password or "") < 8:
        raise ValueError("Mot de passe trop court (minimum 8 caracteres)")
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


def update_user(user_id, nom=None, prenom=None, role=None, est_actif=None):
    ensure_schema()
    cur_user = get_user(user_id)
    if not cur_user:
        return False
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


def set_password(user_id, new_password, resetter=None):
    ensure_schema()
    if len(new_password or "") < 8:
        raise ValueError("Mot de passe trop court (minimum 8 caracteres)")
    cur_user = get_user(user_id)
    if not cur_user:
        raise ValueError("Compte introuvable")
    with sqlite_db.get_cursor() as cur:
        cur.execute(
            "UPDATE utilisateurs SET mot_de_passe = ?, failed_attempts = 0, locked_until = NULL, "
            "updated_at = datetime('now') WHERE id = ?",
            (hash_password(new_password), user_id),
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
        import database
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


def authenticate(email, password, ip=""):
    ensure_schema()
    migrate_admin_from_config()
    email = (email or "").strip().lower()
    row = get_user_by_email(email)
    generic = "Identifiants incorrects"
    if row is None:
        time.sleep(0.4)
        audit("connexion.echec", "Email inconnu", email=email, ip=ip)
        return None, generic
    if not row["est_actif"]:
        audit("connexion.refusee", "Compte desactive", email=email, ip=ip)
        return None, "Compte desactive - contactez l'administrateur"
    locked = row["locked_until"]
    if locked:
        try:
            locked_dt = datetime.strptime(locked, _TS_FORMAT)
            if locked_dt > _now():
                mine = ""
                return None, "Compte temporairement verrouille (trop de tentatives)"
        except Exception:
            pass
    cfg = get_auth_config()
    provider = cfg.get("provider", "local")
    pwd_ok = None
    if provider == "sage":
        pwd_ok = _sage_verify_password(email, password)
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
                "nom": row["nom"] or "", "prenom": row["prenom"] or ""}, None
    attempts = (row["failed_attempts"] or 0) + 1
    lock_until = None
    if attempts >= cfg.get("max_attempts", 5):
        lock_until = _ts(_now() + timedelta(minutes=cfg.get("lock_minutes", 15)))
        audit("connexion.verrouille", "Compte verrouille apres {} echecs".format(attempts),
              email=email, ip=ip)
    with sqlite_db.get_cursor() as cur:
        cur.execute(
            "UPDATE utilisateurs SET failed_attempts = ?, locked_until = ? WHERE id = ?",
            (attempts, lock_until, row["id"]),
        )
    audit("connexion.echec", "Mot de passe invalide (tentative {})".format(attempts),
          email=email, ip=ip)
    if lock_until:
        return None, "Compte verrouille pendant {} minutes".format(cfg.get("lock_minutes", 15))
    return None, generic


def audit(action, detail="", email="", actor=None, ip=""):
    try:
        ensure_schema()
        with sqlite_db.get_cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log (utilisateur, action, detail, ip) VALUES (?, ?, ?, ?)",
                (actor or email or "", action, detail[:500], ip or ""),
            )
    except Exception as e:
        logger.warning("Audit log impossible: %s", e)


def list_audit(limit=200):
    ensure_schema()
    with sqlite_db.get_cursor() as cur:
        cur.execute(
            "SELECT id, ts, utilisateur, action, detail, ip FROM audit_log "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (int(limit),),
        )
        return [dict(r) for r in cur.fetchall()]