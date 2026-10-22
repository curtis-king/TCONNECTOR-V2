import os
import sys
import json
import time
import sqlite3
import logging
import threading
import weakref
from datetime import datetime
from contextlib import contextmanager

logger = logging.getLogger("t-connector.sqlite")

from app.core.paths import data_dir as _uses_data_dir  # noqa: F401

DATA_DIR = _uses_data_dir()
# Chemin surchargeable pour les tests isolés (jamais la base de dev).
DB_PATH = os.environ.get("TCONNECTOR_DB_PATH") or os.path.join(DATA_DIR, "tconnector.db")

# Une connexion sqlite3 n'est PAS reentrante : la partager entre les
# threads Flask, le scheduler, les workers de certification et le watcher
# provoquait des SystemError sur commit() et des transactions perdues
# (audit 2026-10-21). Connexion dediee par thread, WAL pour la concurrence
# lecteur/lecteur + writer unique.
_local = threading.local()
# Cle = thread (weakref) : les connexions des workers ephemères ne sont pas
# retenues apres la mort du thread. Sert uniquement a l'arret propre.
_all_conns = weakref.WeakKeyDictionary()
_all_lock = threading.Lock()


def _get_db_path():
    os.makedirs(DATA_DIR, exist_ok=True)
    return DB_PATH


def get_connection():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            _local.conn = None
            with _all_lock:
                _all_conns.pop(threading.current_thread(), None)

    path = _get_db_path()
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    _local.conn = conn
    with _all_lock:
        _all_conns[threading.current_thread()] = conn
    logger.info("SQLite connecte (thread=%s): %s",
                threading.current_thread().name, path)
    return conn


def close_connection():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None
        with _all_lock:
            _all_conns.pop(threading.current_thread(), None)


def close_all_connections():
    """Ferme toutes les connexions connues (arret propre du process)."""
    with _all_lock:
        conns = list(_all_conns.values())
        _all_conns.clear()
    for conn in conns:
        try:
            conn.close()
        except Exception:
            pass


@contextmanager
def get_cursor():
    conn = get_connection()
    cur = conn.cursor()
    try:
        yield cur
        # Ne pas committer un contexte en lecture seule : cela provoquait
        # "cannot commit - no transaction is active" sur les SELECT.
        if conn.in_transaction:
            conn.commit()
    except Exception:
        if conn.in_transaction:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        cur.close()


def execute_with_retry(cur, sql, params=(), attempts=3):
    """Execute une ecriture en tolerant un verrou SQLite transitoire."""
    last = None
    for i in range(attempts):
        try:
            cur.execute(sql, params)
            return
        except sqlite3.OperationalError as e:
            last = e
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            time.sleep(0.2 * (2 ** i))
    raise last


SCHEMA = """
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rccm TEXT DEFAULT '',
                is_taxable INTEGER DEFAULT 1,
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
                payment_method TEXT DEFAULT 'bank_transfer',
                devise TEXT DEFAULT 'XAF',
                montant_ht_brut REAL DEFAULT 0.0,
                total_tax_t_amount REAL DEFAULT 0.0,
                total_tax_r_amount REAL DEFAULT 0.0,
                total_exempt_amount REAL DEFAULT 0.0,
                discount_amount REAL DEFAULT 0.0,
                total_line_discount_amount REAL DEFAULT 0.0,
                additional_cent_tax REAL DEFAULT 0.0,
                electronic_stamp_duty REAL DEFAULT 0.0,
                is_recipient_taxable INTEGER DEFAULT 1,
                recipient_rccm TEXT DEFAULT '',
                reference_invoice_id TEXT DEFAULT '',
                payment_date TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (contact_id) REFERENCES contacts(id),
                FOREIGN KEY (vendeur_id) REFERENCES vendeurs(id)
            );

            CREATE TABLE IF NOT EXISTS sfec_invoice_links (
                numero TEXT PRIMARY KEY,
                parent_numero TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_sfec_invoice_links_parent
                ON sfec_invoice_links(parent_numero);

            CREATE TABLE IF NOT EXISTS sfec_anomalies (
                numero TEXT PRIMARY KEY,
                motif TEXT NOT NULL DEFAULT '',
                detected_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_sfec_anomalies_detected
                ON sfec_anomalies(detected_at DESC);

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
                subtotal REAL DEFAULT 0.0,
                discount_type TEXT DEFAULT 'fixed',
                type_article TEXT DEFAULT 'product',
                classification_code TEXT DEFAULT '',
                nb_colis REAL DEFAULT 0.0,
                conditionnement TEXT DEFAULT '',
                poids_kg REAL DEFAULT 0.0,
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

            -- Anti double-validation POS (PO-25/PO-63) : une cle d'idempotence
            -- fournie par le client ne cree qu'un seul ticket.
            CREATE TABLE IF NOT EXISTS pos_idempotency (
                key TEXT PRIMARY KEY,
                ticket_id INTEGER NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
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
            CREATE INDEX IF NOT EXISTS idx_invoices_type_doc ON invoices(type_doc);
            CREATE INDEX IF NOT EXISTS idx_invoices_reference_invoice_id ON invoices(reference_invoice_id);
            CREATE INDEX IF NOT EXISTS idx_invoices_contact_id ON invoices(contact_id);
            CREATE INDEX IF NOT EXISTS idx_invoices_vendeur_id ON invoices(vendeur_id);
            CREATE INDEX IF NOT EXISTS idx_invoices_tiers_code ON invoices(tiers_code);
            CREATE INDEX IF NOT EXISTS idx_invoices_numero_source ON invoices(numero, source);
            CREATE INDEX IF NOT EXISTS idx_invoices_push_sage ON invoices(synced_sage, source, statut, date_facture);
            CREATE INDEX IF NOT EXISTS idx_invoices_pos_retry ON invoices(source, synced_sage, sfec_statut, date_facture);
            CREATE INDEX IF NOT EXISTS idx_invoices_certif_list ON invoices(statut, sfec_statut, date_facture);
            CREATE INDEX IF NOT EXISTS idx_invoices_list ON invoices(type_doc, statut, source, date_facture);
            CREATE INDEX IF NOT EXISTS idx_invoices_sfec_num ON invoices(sfec_num_certif);
            CREATE INDEX IF NOT EXISTS idx_invoice_lines_invoice_ligne ON invoice_lines(invoice_id, numero_ligne);
            CREATE INDEX IF NOT EXISTS idx_invoice_lines_product ON invoice_lines(product_id);
            CREATE INDEX IF NOT EXISTS idx_pos_ticket_lines_ticket_ligne ON pos_ticket_lines(ticket_id, numero_ligne);
            CREATE INDEX IF NOT EXISTS idx_pos_ticket_lines_product ON pos_ticket_lines(product_id);
            CREATE INDEX IF NOT EXISTS idx_pos_tickets_statut ON pos_tickets(statut);
            CREATE INDEX IF NOT EXISTS idx_pos_tickets_statut_date ON pos_tickets(statut, date_ticket);
            CREATE INDEX IF NOT EXISTS idx_pos_tickets_vendeur_statut_date ON pos_tickets(vendeur_id, statut, date_ticket);
            CREATE INDEX IF NOT EXISTS idx_pos_tickets_invoice ON pos_tickets(invoice_id);
            CREATE INDEX IF NOT EXISTS idx_pos_tickets_contact ON pos_tickets(contact_id);
            CREATE INDEX IF NOT EXISTS idx_contacts_synced_code ON contacts(synced_sage, code);
            CREATE INDEX IF NOT EXISTS idx_contacts_type_actif_nom ON contacts(type, est_actif, nom);
            CREATE INDEX IF NOT EXISTS idx_products_actif_desig ON products(est_actif, designation);
            CREATE INDEX IF NOT EXISTS idx_products_famille ON products(famille);
            CREATE INDEX IF NOT EXISTS idx_products_barcode_actif ON products(barcode, est_actif);
            CREATE INDEX IF NOT EXISTS idx_products_sage_ar_ref ON products(sage_ar_ref);
            CREATE INDEX IF NOT EXISTS idx_vendeurs_actif_nom ON vendeurs(est_actif, nom);

            CREATE TABLE IF NOT EXISTS invoice_print_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                numero TEXT NOT NULL,
                canal TEXT DEFAULT '',
                mention TEXT DEFAULT '',
                printed_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_invoice_print_log_numero ON invoice_print_log(numero);
                        CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_avoir_unique
                ON invoices(reference_invoice_id)
                WHERE type_doc = 'avoir' AND reference_invoice_id <> '';
            CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_niu_unique
                ON contacts(niu) WHERE niu <> '';
"""


def init_database():
    with get_cursor() as cur:
        cur.executescript(SCHEMA)
    _seed_default_tax_rates()
    _seed_default_settings()
    _migrate()
    logger.info("Base SQLite initialisee")


def _column_exists(cur, table, column):
    cur.execute("PRAGMA table_info({})".format(table))
    for row in cur.fetchall():
        if row["name"] == column:
            return True
    return False


def _migrate():
    migrations = [
        ("pos_ticket_lines", [
            ("remise_pct", "REAL DEFAULT 0.0"),
            ("remise_montant", "REAL DEFAULT 0.0"),
        ]),
        ("pos_tickets", [
            ("remise_globale_pct", "REAL DEFAULT 0.0"),
            ("remise_globale_montant", "REAL DEFAULT 0.0"),
        ]),
        ("invoices", [
            ("payment_method", "TEXT DEFAULT 'bank_transfer'"),
            ("devise", "TEXT DEFAULT 'XAF'"),
            ("montant_ht_brut", "REAL DEFAULT 0.0"),
            ("total_tax_t_amount", "REAL DEFAULT 0.0"),
            ("total_tax_r_amount", "REAL DEFAULT 0.0"),
            ("total_exempt_amount", "REAL DEFAULT 0.0"),
            ("discount_amount", "REAL DEFAULT 0.0"),
            ("total_line_discount_amount", "REAL DEFAULT 0.0"),
            ("additional_cent_tax", "REAL DEFAULT 0.0"),
            ("electronic_stamp_duty", "REAL DEFAULT 0.0"),
            ("is_recipient_taxable", "INTEGER DEFAULT 1"),
            ("recipient_rccm", "TEXT DEFAULT ''"),
            ("reference_invoice_id", "TEXT DEFAULT ''"),
            ("payment_date", "TEXT DEFAULT ''"),
        ]),
        ("invoice_lines", [
            ("subtotal", "REAL DEFAULT 0.0"),
            ("discount_type", "TEXT DEFAULT 'fixed'"),
            ("type_article", "TEXT DEFAULT 'product'"),
            ("classification_code", "TEXT DEFAULT ''"),
            ("nb_colis", "REAL DEFAULT 0.0"),
            ("conditionnement", "TEXT DEFAULT ''"),
            ("poids_kg", "REAL DEFAULT 0.0"),
        ]),
        ("contacts", [
            ("rccm", "TEXT DEFAULT ''"),
            ("is_taxable", "INTEGER DEFAULT 1"),
        ]),
    ]
    with get_cursor() as cur:
        for table, columns in migrations:
            for col, definition in columns:
                if _column_exists(cur, table, col):
                    continue
                try:
                    cur.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, col, definition))
                    logger.info("Migration: %s.%s ajoutee", table, col)
                except Exception as e:
                    logger.warning("Migration: %s.%s deja presente (%s)", table, col, e)

        # Index uniques (BUG-003 anti-double-avoir, BUG-005 doublons NIU)
        try:
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_avoir_unique
                ON invoices(reference_invoice_id)
                WHERE type_doc = 'avoir' AND reference_invoice_id <> ''
            """)
        except Exception as e:
            logger.warning("Migration: index idx_invoices_avoir_unique - %s", e)

        try:
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_niu_unique
                ON contacts(niu) WHERE niu <> ''
            """)
        except Exception as e:
            logger.warning(
                "Migration: index idx_contacts_niu_unique - echec, verifier les doublons NIU (%s)", e
            )

        # Print hijacking (watch dossier PDF) : registre anti-redondance.
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS processed_pdf_prints (
                    file_hash TEXT PRIMARY KEY,
                    numero TEXT DEFAULT '',
                    src_path TEXT DEFAULT '',
                    out_path TEXT DEFAULT '',
                    printer TEXT DEFAULT '',
                    status TEXT DEFAULT '',
                    error TEXT DEFAULT '',
                    printed_at TEXT DEFAULT (datetime('now'))
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_processed_pdf_prints_numero
                ON processed_pdf_prints(numero, printed_at)
            """)
            # Colonnes certification (ajout non destructif sur bases existantes).
            for col in ("sfec_statut", "sfec_num_certif", "first_seen_at"):
                try:
                    cur.execute(
                        "ALTER TABLE processed_pdf_prints ADD COLUMN {} TEXT DEFAULT ''".format(col)
                    )
                except Exception:
                    pass  # colonne deja presente
        except Exception as e:
            logger.warning("Migration: table processed_pdf_prints - %s", e)

        # Migration settings avoirs : anciennes cles erronees -> cles seedees
        for old_key, new_key, default in (
            ("doc_format_avoir", "avoir_format", "AV{:06d}"),
            ("doc_next_avoir", "avoir_next_number", "1"),
        ):
            cur.execute("SELECT value FROM settings WHERE key = ?", (old_key,))
            old_row = cur.fetchone()
            cur.execute("SELECT value FROM settings WHERE key = ?", (new_key,))
            new_row = cur.fetchone()
            new_val = new_row["value"] if new_row else ""
            if not new_val:
                valeur = old_row["value"] if (old_row and old_row["value"]) else default
                if new_row:
                    cur.execute(
                        "UPDATE settings SET value = ?, updated_at = datetime('now') WHERE key = ?",
                        (valeur, new_key)
                    )
                else:
                    cur.execute(
                        "INSERT INTO settings (key, value) VALUES (?, ?)",
                        (new_key, valeur)
                    )
            cur.execute("DELETE FROM settings WHERE key = ?", (old_key,))

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
        "avoir_prefix": "AV",
        "avoir_format": "AV{:06d}",
        "avoir_next_number": "1",
        "cent_tax_rate": "5",
        "stamp_duty_rate": "0"

    }
    with get_cursor() as cur:
        for key, value in defaults.items():
            cur.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )
    # CF-09 : au premier demarrage, la numerotation effective (settings) est
    # initialisee depuis la config company si celle-ci la personnalise.
    try:
        from app.config.manager import get_config
        company = get_config().get("company", {}) or {}
        prefix = str(company.get("invoice_prefix") or "").strip()
        next_no = company.get("invoice_next_number")
        with get_cursor() as cur:
            if prefix and prefix != "FA":
                cur.execute("SELECT value FROM settings WHERE key = 'invoice_prefix'")
                row = cur.fetchone()
                if row and row["value"] == "FA":
                    cur.execute("UPDATE settings SET value = ? WHERE key = 'invoice_prefix'", (prefix,))
                    cur.execute("UPDATE settings SET value = ? WHERE key = 'invoice_format'",
                                (prefix + "{:06d}",))
            if next_no:
                cur.execute("SELECT value FROM settings WHERE key = 'invoice_next_number'")
                row = cur.fetchone()
                if row and row["value"] == "1" and int(next_no) > 1:
                    cur.execute("UPDATE settings SET value = ? WHERE key = 'invoice_next_number'",
                                (str(int(next_no)),))
    except Exception:
        pass


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


def cent_tax_rate():
    """Taux des centimes additionnels, en % (configurable, defaut 5)."""
    try:
        return float(get_setting("cent_tax_rate", "5") or 5)
    except (TypeError, ValueError):
        return 5.0


def compute_cent_tax(tva_collectee):
    """Centimes additionnels = taux % de la TVA COLLECTEE (toutes tranches).

    Regle legale : decret n° 2023-1737, art. 22 — « la base d'imposition des
    centimes additionnels est le montant de la TVA collectee. Le taux
    applicable est de cinq pourcent (5 %). » La base inclut donc la TVA a 18 %
    ET la TVA a taux reduit (5 %), pas seulement la tranche 18 %.
    """
    return round((tva_collectee or 0) * cent_tax_rate() / 100.0, 2)


def get_sfec_invoice_link(numero):
    """Parent manuel d'un avoir (table sfec_invoice_links), ou ''."""
    numero = str(numero or "").strip()
    if not numero:
        return ""
    with get_cursor() as cur:
        cur.execute(
            "SELECT parent_numero FROM sfec_invoice_links WHERE numero = ?",
            (numero,),
        )
        row = cur.fetchone()
    return (row["parent_numero"] if row else "") or ""


def fetch_sfec_invoice_links():
    """Tous les liens manuels avoir -> facture d'origine."""
    with get_cursor() as cur:
        cur.execute("SELECT numero, parent_numero FROM sfec_invoice_links")
        return {
            row["numero"]: row["parent_numero"]
            for row in cur.fetchall()
            if row["numero"] and row["parent_numero"]
        }


def set_sfec_invoice_link(numero, parent_numero):
    """Enregistre/remplace le parent manuel d'un avoir."""
    numero = str(numero or "").strip()
    parent_numero = str(parent_numero or "").strip()
    if not numero or not parent_numero:
        raise ValueError("numero et parent_numero sont obligatoires")
    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO sfec_invoice_links (numero, parent_numero)
            VALUES (?, ?)
            ON CONFLICT(numero) DO UPDATE SET
                parent_numero = excluded.parent_numero,
                created_at = datetime('now')
        """, (numero, parent_numero))
    return True


def record_sfec_anomaly(numero, motif):
    """Enregistre/actualise le motif d'une facture A_SURVEILLER."""
    numero = str(numero or "").strip()
    if not numero:
        return False
    motif = str(motif or "").strip() or "verification requise"
    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO sfec_anomalies (numero, motif)
            VALUES (?, ?)
            ON CONFLICT(numero) DO UPDATE SET
                motif = excluded.motif,
                detected_at = datetime('now')
        """, (numero, motif))
    return True


def fetch_sfec_anomalies(limit=500):
    """Liste les anomalies SFEC, avec les montants du miroir si disponibles."""
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 500
    with get_cursor() as cur:
        cur.execute("""
            SELECT a.numero, a.motif, a.detected_at,
                   i.montant_ttc, i.tiers_nom, i.date_facture
            FROM sfec_anomalies a
            LEFT JOIN invoices i ON i.numero = a.numero
            ORDER BY a.detected_at DESC, a.numero
            LIMIT ?
        """, (limit,))
        return [dict(row) for row in cur.fetchall()]


def get_sfec_anomaly(numero):
    numero = str(numero or "").strip()
    if not numero:
        return None
    with get_cursor() as cur:
        cur.execute(
            "SELECT numero, motif, detected_at FROM sfec_anomalies WHERE numero = ?",
            (numero,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def clear_sfec_anomaly(numero):
    """Retire l'anomalie apres certification reussie."""
    numero = str(numero or "").strip()
    if not numero:
        return 0
    with get_cursor() as cur:
        cur.execute("DELETE FROM sfec_anomalies WHERE numero = ?", (numero,))
        return cur.rowcount or 0


def update_invoice_sfec(invoice_id, sfec_id="", certification_number="",
                        signature="", qr_code="", certification_date="", statut=""):
    """Enregistre les donnees de certification SFEC d'une facture locale."""
    with get_cursor() as cur:
        cur.execute("""
            UPDATE invoices SET
                sfec_id = CASE WHEN ? <> '' THEN ? ELSE sfec_id END,
                sfec_num_certif = CASE WHEN ? <> '' THEN ? ELSE sfec_num_certif END,
                sfec_signature = CASE WHEN ? <> '' THEN ? ELSE sfec_signature END,
                sfec_qr_code = CASE WHEN ? <> '' THEN ? ELSE sfec_qr_code END,
                sfec_date_certif = CASE WHEN ? <> '' THEN ? ELSE sfec_date_certif END,
                sfec_statut = CASE WHEN ? <> '' THEN ? ELSE sfec_statut END,
                updated_at = datetime('now')
            WHERE id = ?
        """, (
            sfec_id, sfec_id,
            certification_number, certification_number,
            signature, signature,
            qr_code, qr_code,
            certification_date, certification_date,
            statut, statut,
            invoice_id
        ))
    return True


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

def generate_avoir_number():
    fmt = get_setting("avoir_format") or "AV{:06d}"
    with get_cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key = 'avoir_next_number'")
        row = cur.fetchone()
        try:
            num = int(row["value"]) if row and row["value"] else 1
        except (TypeError, ValueError):
            num = 1
        while True:
            numero = fmt.format(num)
            cur.execute("SELECT id FROM invoices WHERE numero = ?", (numero,))
            if not cur.fetchone():
                cur.execute(
                    "UPDATE settings SET value = ?, updated_at = datetime('now') WHERE key = 'avoir_next_number'",
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


def calc_line_ticket_totals(qty, unit_price, remise_pct, remise_montant, tax_rate):
    subtotal = round(qty * unit_price, 2)
    remise_pct_amt = round(subtotal * (remise_pct or 0) / 100, 2)
    remise_total = round((remise_pct_amt + (remise_montant or 0)), 2)
    net_ht = round(subtotal - remise_total, 2)
    if net_ht < 0:
        net_ht = 0.0
    tva = round(net_ht * tax_rate / 100, 2)
    ttc = round(net_ht + tva, 2)
    return net_ht, tva, ttc, remise_total


def recalc_invoice_totals(invoice_id):
    with get_cursor() as cur:
        cur.execute("""
            SELECT
                COALESCE(SUM(montant_ht), 0) as total_ht,
                COALESCE(SUM(montant_tva), 0) as total_tva,
                COALESCE(SUM(montant_ttc), 0) as total_ttc,
                COALESCE(SUM(subtotal), 0) as total_brut,
                COALESCE(SUM(subtotal - montant_ht), 0) as total_remise_lignes,
                COALESCE(SUM(CASE WHEN taux_tva = 18 THEN montant_tva ELSE 0 END), 0) as tva_t,
                COALESCE(SUM(CASE WHEN taux_tva = 5 THEN montant_tva ELSE 0 END), 0) as tva_r,
                COALESCE(SUM(CASE WHEN taux_tva = 0 THEN montant_ht ELSE 0 END), 0) as exonere
            FROM invoice_lines WHERE invoice_id = ?
        """, (invoice_id,))
        row = cur.fetchone()
        if row:
            try:
                stamp_rate = float(get_setting("stamp_duty_rate", "0") or 0)
            except (TypeError, ValueError):
                stamp_rate = 0.0
            # Centimes additionnels : 5 % de la TVA collectee (tva 18 % + tva
            # reduite), decret n° 2023-1737 art. 22 — cf. compute_cent_tax().
            additional_cent_tax = compute_cent_tax(row["tva_t"] + row["tva_r"])
            stamp_duty = round(row["total_ttc"] * stamp_rate / 100.0, 2) if stamp_rate else 0.0
            montant_ttc = round(row["total_ttc"] + additional_cent_tax + stamp_duty, 2)
            cur.execute("SELECT montant_ttc, montant_restant FROM invoices WHERE id = ?", (invoice_id,))
            ancien = cur.fetchone()
            deja_paye = 0.0
            if ancien:
                deja_paye = max(round((ancien["montant_ttc"] or 0) - (ancien["montant_restant"] or 0), 2), 0.0)
            montant_restant = max(round(montant_ttc - deja_paye, 2), 0.0)
            cur.execute("""
                UPDATE invoices SET
                    montant_ht = ?, montant_tva = ?, montant_ttc = ?,
                    montant_restant = ?,
                    montant_ht_brut = ?,
                    total_line_discount_amount = ?,
                    total_tax_t_amount = ?,
                    total_tax_r_amount = ?,
                    total_exempt_amount = ?,
                    additional_cent_tax = ?,
                    electronic_stamp_duty = ?,
                    updated_at = datetime('now')
                WHERE id = ?
            """, (
                row["total_ht"], row["total_tva"], montant_ttc,
                montant_restant,
                row["total_brut"],
                row["total_remise_lignes"],
                row["tva_t"],
                row["tva_r"],
                row["exonere"],
                additional_cent_tax,
                stamp_duty,
                invoice_id
            ))

            
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
                SELECT
                    COALESCE(remise_globale_pct, 0) as r_pct,
                    COALESCE(remise_globale_montant, 0) as r_montant
                FROM pos_tickets WHERE id = ?
            """, (ticket_id,))
            trow = cur.fetchone()
            total_ht = row["total_ht"]
            total_tva = row["total_tva"]
            total_ttc = row["total_ttc"]
            r_pct = trow["r_pct"] if trow else 0.0
            r_montant = trow["r_montant"] if trow else 0.0
            if r_montant or r_pct:
                base = total_ttc
                global_remise_ttc = round(base * r_pct / 100, 2) if r_pct else 0.0
                applied_remise_ttc = global_remise_ttc + r_montant
                # Recompute HT/TVA proportionally to keep VAT correct
                if total_ttc and total_ttc > 0:
                    ratio = max((total_ttc - applied_remise_ttc) / total_ttc, 0)
                else:
                    ratio = 0
                total_ht = round(total_ht * ratio, 2)
                total_tva = round(total_tva * ratio, 2)
                total_ttc = max(round(total_ttc - applied_remise_ttc, 2), 0.0)
            cur.execute("""
                UPDATE pos_tickets SET
                    montant_ht = ?, montant_tva = ?, montant_ttc = ?
                WHERE id = ?
            """, (total_ht, total_tva, total_ttc, ticket_id))
            return {"montant_ht": total_ht, "montant_tva": total_tva, "montant_ttc": total_ttc}
    return {"montant_ht": 0, "montant_tva": 0, "montant_ttc": 0}


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
