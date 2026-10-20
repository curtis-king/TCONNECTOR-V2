"""Gestion multi-sites legere (fichier JSON, pas de table SQL).

Chaque site = (users[] ou machine) -> imprimante A4 (factures Sage) +
imprimante ticket 80 mm (POS). Le site actif est resolu a chaque impression
depuis la session Windows en cours, puis le hostname. Aucun match -> pas
d'impression (quarantaine). Seules les imprimantes declarees dans un site
actif sont utilisables (allowlist).

Switches globaux (meme fichier) : paused (kill-switch) et dry_run.
"""
import getpass
import json
import logging
import os
import socket
import threading
import uuid

from app.core.paths import data_dir

logger = logging.getLogger("t-connector.print_hijack.sites")

_lock = threading.RLock()
SITES_FILE = os.path.join(data_dir(), "sites.json")

_DEFAULT = {"paused": False, "dry_run": False, "sites": []}


def _ensure_file():
    if not os.path.exists(SITES_FILE):
        save_config_dict(dict(_DEFAULT))
    return SITES_FILE


def load_config_dict():
    """Charge sites.json (structure : paused, dry_run, sites[])."""
    with _lock:
        try:
            _ensure_file()
            with open(SITES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("racine JSON invalide")
            data.setdefault("paused", False)
            data.setdefault("dry_run", False)
            sites = data.get("sites")
            if not isinstance(sites, list):
                data["sites"] = []
            return data
        except Exception as e:  # noqa: BLE001
            logger.error("Lecture %s impossible: %s", SITES_FILE, e)
            return dict(_DEFAULT)


def save_config_dict(data):
    with _lock:
        os.makedirs(os.path.dirname(SITES_FILE), exist_ok=True)
        tmp = SITES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SITES_FILE)


def current_user():
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return ""


def current_machine():
    try:
        return socket.gethostname()
    except Exception:  # noqa: BLE001
        return ""


def is_paused():
    return bool(load_config_dict().get("paused"))


def is_dry_run():
    return bool(load_config_dict().get("dry_run"))


def list_sites():
    return load_config_dict().get("sites", [])


def get_site(site_id):
    for site in list_sites():
        if site.get("id") == site_id:
            return site
    return None


def resolve_active_site():
    """Resout le site actif : user session -> hostname -> site unique actif.

    Retourne (site_dict | None, raison).
    """
    data = load_config_dict()
    sites = [s for s in data.get("sites", []) if s.get("enabled", True)]
    if not sites:
        return None, "aucun site configure ou actif"

    user = (current_user() or "").lower()
    machine = (current_machine() or "").lower()

    for site in sites:
        users = [str(u).strip().lower() for u in (site.get("users") or []) if str(u).strip()]
        if user and user in users:
            return site, "match user '{}'".format(user)

    for site in sites:
        site_machine = str(site.get("machine") or "").strip().lower()
        if site_machine and machine and site_machine == machine:
            return site, "match machine '{}'".format(machine)

    if len(sites) == 1:
        return sites[0], "site unique (fallback)"

    return None, "aucun site ne matche user='{}' machine='{}'".format(user, machine)


def site_printer(site, kind="a4"):
    """Imprimante d'un site : kind='a4' (factures) ou 'ticket' (80 mm)."""
    if not site:
        return ""
    key = "a4_printer" if kind == "a4" else "ticket_printer"
    return str(site.get(key) or "").strip()


def enumerate_printers():
    """Imprimantes reelles vues par Windows (liste vide si indisponible)."""
    try:
        import win32print
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        return sorted({p[2] for p in win32print.EnumPrinters(flags)})
    except Exception as e:  # noqa: BLE001
        logger.debug("EnumPrinters indisponible: %s", e)
        return []


def printer_connected(name):
    """True si l'imprimante existe (ou si l'enum n'est pas dispo = best effort)."""
    if not name:
        return False
    printers = enumerate_printers()
    if not printers:
        try:
            from app.print_hijack import auto_printer
            return auto_printer.printer_exists(name)
        except Exception:  # noqa: BLE001
            return True
    return name in printers


def upsert_site(site):
    """Cree ou met a jour un site (dict avec id optionnel). Retourne le site sauve."""
    data = load_config_dict()
    site = dict(site or {})
    site_id = str(site.get("id") or "").strip()
    sites = data.get("sites", [])
    if site_id:
        for i, s in enumerate(sites):
            if s.get("id") == site_id:
                merged = dict(s)
                merged.update({k: v for k, v in site.items() if k != "id"})
                merged["id"] = site_id
                sites[i] = merged
                save_config_dict(data)
                return merged
    site["id"] = site_id or uuid.uuid4().hex[:12]
    site.setdefault("name", "Site sans nom")
    site.setdefault("users", [])
    site.setdefault("machine", "")
    site.setdefault("a4_printer", "")
    site.setdefault("ticket_printer", "")
    site.setdefault("enabled", True)
    sites.append(site)
    save_config_dict(data)
    return site


def delete_site(site_id):
    data = load_config_dict()
    before = len(data.get("sites", []))
    data["sites"] = [s for s in data.get("sites", []) if s.get("id") != site_id]
    save_config_dict(data)
    return len(data["sites"]) < before


def set_switches(paused=None, dry_run=None):
    data = load_config_dict()
    if paused is not None:
        data["paused"] = bool(paused)
    if dry_run is not None:
        data["dry_run"] = bool(dry_run)
    save_config_dict(data)
    return {"paused": bool(data.get("paused")), "dry_run": bool(data.get("dry_run"))}


def switches():
    data = load_config_dict()
    return {"paused": bool(data.get("paused")), "dry_run": bool(data.get("dry_run"))}
