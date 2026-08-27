import os
import sys
import json
import time
import sqlite3
import logging
import threading
from datetime import datetime
from contextlib import contextmanager

logger = logging.getLogger("t-connector.sqlite")

if getattr(sys, 'frozen', False):
    _BASE_DIR = os.path.dirname(sys.executable)
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(_BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "tconnector.db")

_lock = threading.Lock()
_conn = None


def _get_db_path():
    os.makedirs(DATA_DIR, exist_ok=True)
    return DB_PATH


def get_connection():
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.execute("SELECT 1")
                return _conn
            except Exception:
                try:
                    _conn.close()
                except Exception:
                    pass
                _conn = None

        path = _get_db_path()
        _conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.execute("PRAGMA busy_timeout=5000")
        logger.info("SQLite connecte: %s", path)
        return _conn


def close_connection():
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None


@contextmanager
def get_cursor():
    conn = get_connection()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cur.close()


SCHEMA = """
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                nom TEXT NOT NULL DEFAULT '',
                type TEXT NOT NULL DEFAULT 'client',
                email TEXT DEFAULT '',
                telephone TEXT DEFAULT '',
                niu TEXT DEFAULT '',
                adresse TEXT DEFAULT '',
                ville TEXT DEFAULT '',
                pays TEXT DEFAULT 'CG',
                code_postal TEXT DEFAULT '',
                siret TEXT DEFAULT '',
                type_raw TEXT DEFAULT '',
                est_actif INTEGER DEFAULT 1,
                synced_sage INTEGER DEFAULT 0,
                sage_ct_num TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ref TEXT UNIQUE NOT NULL,
                barcode TEXT DEFAULT '',
                designation TEXT NOT NULL DEFAULT '',
                famille TEXT DEFAULT '',
                nature INTEGER DEFAULT 0,
                prix_vente REAL DEFAULT 0.0,
                prix_achat REAL DEFAULT 0.0,
                tva_code TEXT DEFAULT '18',
                unite TEXT DEFAULT 'U',
                stock_reel REAL DEFAULT 0,
                est_actif INTEGER DEFAULT 1,
                synced_sage INTEGER DEFAULT 0,
                sage_ar_ref TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS vendeurs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                nom TEXT NOT NULL DEFAULT '',
                prenom TEXT DEFAULT '',
                email TEXT DEFAULT '',
                telephone TEXT DEFAULT '',
                role TEXT DEFAULT 'vendeur',
                est_actif INTEGER DEFAULT 1,
                pin TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS tax_rates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                libelle TEXT DEFAULT '',
                taux REAL NOT NULL DEFAULT 18.0,
                est_actif INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                numero TEXT UNIQUE NOT NULL,
                date_facture TEXT NOT NULL,
                date_echeance TEXT DEFAULT '',
                reference TEXT DEFAULT '',
                contact_id INTEGER,
                tiers_code TEXT DEFAULT '',
                tiers_nom TEXT DEFAULT '',
                tiers_niu TEXT DEFAULT '',
                tiers_email TEXT DEFAULT '',
                tiers_telephone TEXT DEFAULT '',
                tiers_adresse TEXT DEFAULT '',
                tiers_type TEXT DEFAULT 'business',
                montant_ht REAL DEFAULT 0.0,
                montant_tva REAL DEFAULT 0.0,
                montant_ttc REAL DEFAULT 0.0,
                montant_restant REAL DEFAULT 0.0,
                statut TEXT DEFAULT 'brouillon',
                valide INTEGER DEFAULT 0,
                type_doc TEXT DEFAULT 'vente',
                source TEXT DEFAULT 'web',
                sfec_statut TEXT DEFAULT '',
                sfec_num_certif TEXT DEFAULT '',
                sfec_signature TEXT DEFAULT '',
                sfec_qr_code TEXT DEFAULT '',
                sfec_date_certif TEXT DEFAULT '',
                sfec_id TEXT DEFAULT '',
                synced_sage INTEGER DEFAULT 0,
                sage_domaine INTEGER DEFAULT 0,
                sage_type INTEGER DEFAULT 6,
                sage_piece TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                vendeur_id INTEGER,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (contact_id) REFERENCES contacts(id),
                FOREIGN KEY (vendeur_id) REFERENCES vendeurs(id)
            );

            CREATE TABLE IF NOT EXISTS invoice_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL,
                numero_ligne INTEGER NOT NULL,
                designation TEXT NOT NULL DEFAULT '',
                quantite REAL DEFAULT 1.0,
                prix_unitaire REAL DEFAULT 0.0,
                remise_pct REAL DEFAULT 0.0,
                remise_montant REAL DEFAULT 0.0,
                montant_ht REAL DEFAULT 0.0,
                taux_tva REAL DEFAULT 18.0,
                montant_tva REAL DEFAULT 0.0,
                montant_ttc REAL DEFAULT 0.0,
                code_article TEXT DEFAULT '',
                code_compte TEXT DEFAULT '',
                famille TEXT DEFAULT '',
                unite TEXT DEFAULT 'U',
                product_id INTEGER,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE,
                FOREIGN KEY (product_id) REFERENCES products(id)
            );

            CREATE TABLE IF NOT EXISTS pos_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                numero TEXT UNIQUE NOT NULL,
                date_ticket TEXT NOT NULL,
                contact_id INTEGER,
                tiers_nom TEXT DEFAULT 'Client comptoir',
                montant_ht REAL DEFAULT 0.0,
                montant_tva REAL DEFAULT 0.0,
                montant_ttc REAL DEFAULT 0.0,
                mode_paiement TEXT DEFAULT 'especes',
                montant_recu REAL DEFAULT 0.0,
                monnaie_rendue REAL DEFAULT 0.0,
                statut TEXT DEFAULT 'valide',
                invoice_id INTEGER,
                vendeur_id INTEGER,
                caissier TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                synced_sage INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (contact_id) REFERENCES contacts(id),
                FOREIGN KEY (invoice_id) REFERENCES invoices(id),
                FOREIGN KEY (vendeur_id) REFERENCES vendeurs(id)
            );

            CREATE TABLE IF NOT EXISTS pos_ticket_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                numero_ligne INTEGER NOT NULL,
                designation TEXT NOT NULL DEFAULT '',
                quantite REAL DEFAULT 1.0,
                prix_unitaire REAL DEFAULT 0.0,
                montant_ht REAL DEFAULT 0.0,
                taux_tva REAL DEFAULT 18.0,
                montant_tva REAL DEFAULT 0.0,
                montant_ttc REAL DEFAULT 0.0,
                code_article TEXT DEFAULT '',
                barcode TEXT DEFAULT '',
                product_id INTEGER,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (ticket_id) REFERENCES pos_tickets(id) ON DELETE CASCADE,
                FOREIGN KEY (product_id) REFERENCES products(id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT '',
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                table_name TEXT NOT NULL,
                record_id INTEGER,
                record_numero TEXT DEFAULT '',
                action TEXT NOT NULL,
                direction TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                error TEXT DEFAULT '',
                synced_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_invoices_numero ON invoices(numero);
            CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(date_facture);
            CREATE INDEX IF NOT EXISTS idx_invoices_statut ON invoices(statut);
            CREATE INDEX IF NOT EXISTS idx_invoices_sfec ON invoices(sfec_statut);
            CREATE INDEX IF NOT EXISTS idx_invoices_source ON invoices(source);
            CREATE INDEX IF NOT EXISTS idx_invoices_synced ON invoices(synced_sage);
            CREATE INDEX IF NOT EXISTS idx_invoice_lines_invoice ON invoice_lines(invoice_id);
            CREATE INDEX IF NOT EXISTS idx_contacts_code ON contacts(code);
            CREATE INDEX IF NOT EXISTS idx_products_ref ON products(ref);
            CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode);
            CREATE INDEX IF NOT EXISTS idx_pos_tickets_numero ON pos_tickets(numero);
            CREATE INDEX IF NOT EXISTS idx_pos_tickets_date ON pos_tickets(date_ticket);
            CREATE INDEX IF NOT EXISTS idx_pos_tickets_vendeur ON pos_tickets(vendeur_id);
            CREATE INDEX IF NOT EXISTS idx_pos_ticket_lines_ticket ON pos_ticket_lines(ticket_id);
            CREATE INDEX IF NOT EXISTS idx_vendeurs_code ON vendeurs(code);
            CREATE INDEX IF NOT EXISTS idx_sync_log_table ON sync_log(table_name);
            CREATE INDEX IF NOT EXISTS idx_sync_log_status ON sync_log(status);
"""


def init_database():
    with get_cursor() as cur:
        cur.executescript(SCHEMA)
    _seed_default_tax_rates()
    _seed_default_settings()
    logger.info("Base SQLite initialisee")


def _seed_default_tax_rates():
    defaults = [
        ("18", "TVA Standard 18%", 18.0),
        ("5", "TVA Reduite 5%", 5.0),
        ("0", "Exonere 0%", 0.0),
    ]
    with get_cursor() as cur:
        for code, libelle, taux in defaults:
            cur.execute(
                "INSERT OR IGNORE INTO tax_rates (code, libelle, taux) VALUES (?, ?, ?)",
                (code, libelle, taux)
            )


def _seed_default_settings():
    defaults = {
        "invoice_prefix": "FA",
        "invoice_next_number": "1",
        "invoice_format": "FA{:06d}",
        "pos_ticket_prefix": "TK",
        "pos_next_number": "1",
        "pos_format": "TK{:06d}",
        "default_tva_code": "18",
        "company_logo_path": "",
        "ticket_message": "Merci pour votre achat !",
    }
    with get_cursor() as cur:
        for key, value in defaults.items():
            cur.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )


def get_setting(key, default=""):
    with get_cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with get_cursor() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now'))",
            (key, str(value))
        )


def generate_invoice_number():
    prefix = get_setting("invoice_prefix", "FA")
    fmt = get_setting("invoice_format", "FA{:06d}")
    with get_cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key = 'invoice_next_number'")
        row = cur.fetchone()
        num = int(row["value"]) if row else 1
        while True:
            numero = fmt.format(num)
            cur.execute("SELECT id FROM invoices WHERE numero = ?", (numero,))
            if not cur.fetchone():
                cur.execute(
                    "UPDATE settings SET value = ?, updated_at = datetime('now') WHERE key = 'invoice_next_number'",
                    (str(num + 1),)
                )
                return numero
            num += 1


def generate_ticket_number():
    prefix = get_setting("pos_ticket_prefix", "TK")
    fmt = get_setting("pos_format", "TK{:06d}")
    with get_cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key = 'pos_next_number'")
        row = cur.fetchone()
        num = int(row["value"]) if row else 1
        while True:
            numero = fmt.format(num)
            cur.execute("SELECT id FROM pos_tickets WHERE numero = ?", (numero,))
            if not cur.fetchone():
                cur.execute(
                    "UPDATE settings SET value = ?, updated_at = datetime('now') WHERE key = 'pos_next_number'",
                    (str(num + 1),)
                )
                return numero
            num += 1


def calc_line_totals(qty, unit_price, discount_pct, tax_rate):
    subtotal = round(qty * unit_price, 2)
    discount_amount = round(subtotal * discount_pct / 100, 2) if discount_pct else 0.0
    net_ht = round(subtotal - discount_amount, 2)
    tva = round(net_ht * tax_rate / 100, 2)
    ttc = round(net_ht + tva, 2)
    return net_ht, tva, ttc


def recalc_invoice_totals(invoice_id):
    with get_cursor() as cur:
        cur.execute("""
            SELECT
                COALESCE(SUM(montant_ht), 0) as total_ht,
                COALESCE(SUM(montant_tva), 0) as total_tva,
                COALESCE(SUM(montant_ttc), 0) as total_ttc
            FROM invoice_lines WHERE invoice_id = ?
        """, (invoice_id,))
        row = cur.fetchone()
        if row:
            cur.execute("""
                UPDATE invoices SET
                    montant_ht = ?, montant_tva = ?, montant_ttc = ?,
                    montant_restant = ?, updated_at = datetime('now')
                WHERE id = ?
            """, (row["total_ht"], row["total_tva"], row["total_ttc"],
                  row["total_ttc"], invoice_id))


def recalc_ticket_totals(ticket_id):
    with get_cursor() as cur:
        cur.execute("""
            SELECT
                COALESCE(SUM(montant_ht), 0) as total_ht,
                COALESCE(SUM(montant_tva), 0) as total_tva,
                COALESCE(SUM(montant_ttc), 0) as total_ttc
            FROM pos_ticket_lines WHERE ticket_id = ?
        """, (ticket_id,))
        row = cur.fetchone()
        if row:
            cur.execute("""
                UPDATE pos_tickets SET
                    montant_ht = ?, montant_tva = ?, montant_ttc = ?
                WHERE id = ?
            """, (row["total_ht"], row["total_tva"], row["total_ttc"], ticket_id))


def log_sync(table_name, record_id, record_numero, action, direction, status="pending", error=""):
    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO sync_log (table_name, record_id, record_numero, action, direction, status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (table_name, record_id, record_numero, action, direction, status, error))


def row_to_dict(row):
    if row is None:
        return None
    return dict(row)


def rows_to_list(rows):
    return [dict(r) for r in rows]
