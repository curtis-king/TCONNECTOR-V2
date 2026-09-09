import logging
from datetime import datetime
from app.storage.db import get_cursor as get_sqlite_cursor, log_sync

logger = logging.getLogger("t-connector.sage")

_sage_pool = None
_sage_lock = None


def _get_sage_cursor():
    try:
        from app.integration.sage.database import get_cursor as get_sage_cursor
        return get_sage_cursor()
    except Exception as e:
        logger.warning("SQL Server Sage indisponible: %s", e)
        return None


def _get_sage_domaine_type():
    return {"vente_domaine": 0, "vente_type": 6, "achat_domaine": 1}


def write_invoice_to_sage(invoice):
    cursor_ctx = _get_sage_cursor()
    if cursor_ctx is None:
        log_sync("sage_invoice", invoice["id"], invoice["numero"], "write", "sqlite_to_sage",
                 status="error", error="SQL Server indisponible")
        return {"success": False, "error": "SQL Server indisponible"}

    dc = _get_sage_domaine_type()
    numero = invoice["numero"]
    date_str = invoice.get("date_facture", "")
    try:
        date_val = datetime.strptime(date_str[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        date_val = datetime.utcnow()

    tiers_col = "DO_Tiers"
    tiers_val = invoice.get("tiers_code", "")

    try:
        with cursor_ctx as cur:
            sql = """
                INSERT INTO F_DOCENTETE (
                    DO_Domaine, DO_Type, DO_Piece, DO_Date,
                    {tiers_col}, DO_Ref, DO_TotalHT, DO_Taxe1,
                    DO_TotalTTC, DO_NetAPayer, DO_Statut, DO_Valide
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """.format(tiers_col=tiers_col)

            cur.execute(sql, (
                dc["vente_domaine"], dc["vente_type"], numero, date_val,
                tiers_val, invoice.get("reference", ""),
                invoice.get("montant_ht", 0), invoice.get("montant_tva", 0),
                invoice.get("montant_ttc", 0), invoice.get("montant_ttc", 0),
                2, 1
            ))

            lignes = invoice.get("lignes", [])
            for ligne in lignes:
                cur.execute("""
                    INSERT INTO F_DOCLIGNE (
                        DO_Domaine, DO_Type, DO_Piece, DL_Ligne,
                        DL_Design, DL_Qte, DL_PrixUnitaire, DL_MontantHT,
                        DL_Taxe1, DL_MontantTTC, CO_No, AR_Ref
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    dc["vente_domaine"], dc["vente_type"], numero,
                    ligne.get("numero_ligne", 1),
                    ligne.get("designation", ""),
                    ligne.get("quantite", 1),
                    ligne.get("prix_unitaire", 0),
                    ligne.get("montant_ht", 0),
                    ligne.get("taux_tva", 18),
                    ligne.get("montant_ttc", 0),
                    ligne.get("code_compte", ""),
                    ligne.get("code_article", "")
                ))

        with get_sqlite_cursor() as cur:
            cur.execute("""
                UPDATE invoices SET synced_sage = 1, sage_piece = ?, updated_at = datetime('now')
                WHERE id = ?
            """, (numero, invoice["id"]))

        log_sync("sage_invoice", invoice["id"], numero, "write", "sqlite_to_sage", status="ok")
        logger.info("Facture ecrite dans Sage: %s", numero)
        return {"success": True, "numero": numero}

    except Exception as e:
        log_sync("sage_invoice", invoice["id"], numero, "write", "sqlite_to_sage",
                 status="error", error=str(e)[:200])
        logger.error("Ecriture Sage echouee pour %s: %s", numero, e)
        return {"success": False, "error": str(e)[:200]}


def read_invoices_from_sage():
    cursor_ctx = _get_sage_cursor()
    if cursor_ctx is None:
        return []

    try:
        with cursor_ctx as cur:
            cur.execute("""
                SELECT
                    CAST(d.DO_Domaine AS VARCHAR) + '-' + CAST(d.DO_Type AS VARCHAR) + '-' + d.DO_Piece AS id,
                    d.DO_Piece AS numero,
                    ISNULL(TRY_CONVERT(VARCHAR(10), d.DO_Date, 120), '') AS date_facture,
                    ISNULL(d.DO_Tiers, '') AS tiers_code,
                    ISNULL(d.DO_Ref, '') AS reference,
                    ISNULL(d.DO_TotalHT, 0) AS montant_ht,
                    ISNULL(d.DO_TotalTTC, 0) AS montant_ttc,
                    ISNULL(d.DO_NetAPayer, 0) AS montant_restant,
                    d.DO_Statut AS statut_code,
                    CASE
                        WHEN d.DO_Statut = 0 THEN 'SAISI'
                        WHEN d.DO_Statut = 2 THEN 'A COMPTABILISER'
                        WHEN d.DO_Statut = 3 THEN 'COMPTABILISE'
                        ELSE 'AUTRE'
                    END AS statut,
                    ISNULL(d.SFEC_STATUT, '') AS sfec_statut,
                    ISNULL(d.SFEC_NUM_CERTIF, '') AS sfec_num_certif,
                    ISNULL(d.SFEC_SIGNATURE, '') AS sfec_signature,
                    ISNULL(d.SFEC_QR_CODE, '') AS sfec_qr_code
                FROM F_DOCENTETE d
                WHERE d.DO_Domaine = 0 AND d.DO_Type = 6
                ORDER BY d.DO_Date DESC
            """)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()

            invoices = []
            for row in rows:
                inv = dict(zip(columns, [str(v) if v is not None else "" for v in row]))
                try:
                    inv["montant_ht"] = float(str(inv["montant_ht"]).replace(",", "."))
                except (ValueError, TypeError):
                    inv["montant_ht"] = 0
                try:
                    inv["montant_ttc"] = float(str(inv["montant_ttc"]).replace(",", "."))
                except (ValueError, TypeError):
                    inv["montant_ttc"] = 0
                try:
                    inv["montant_restant"] = float(str(inv["montant_restant"]).replace(",", "."))
                except (ValueError, TypeError):
                    inv["montant_restant"] = 0
                try:
                    inv["statut_code"] = int(float(str(inv["statut_code"])))
                except (ValueError, TypeError):
                    inv["statut_code"] = 0
                invoices.append(inv)

            logger.info("Factures Sage lues: %d", len(invoices))
            return invoices

    except Exception as e:
        logger.error("Lecture Sage echouee: %s", e)
        return []


def sync_sage_invoice_to_sqlite(sage_inv):
    numero = sage_inv.get("numero", "")
    if not numero:
        return None

    with get_sqlite_cursor() as cur:
        cur.execute("SELECT id FROM invoices WHERE numero = ? AND source = 'sage'", (numero,))
        existing = cur.fetchone()

        if existing:
            cur.execute("""
                UPDATE invoices SET
                    tiers_code = ?, montant_ht = ?, montant_ttc = ?, montant_restant = ?,
                    sfec_statut = ?, sfec_num_certif = ?, sfec_signature = ?, sfec_qr_code = ?,
                    updated_at = datetime('now')
                WHERE id = ?
            """, (
                sage_inv.get("tiers_code", ""),
                sage_inv.get("montant_ht", 0),
                sage_inv.get("montant_ttc", 0),
                sage_inv.get("montant_restant", 0),
                sage_inv.get("sfec_statut", ""),
                sage_inv.get("sfec_num_certif", ""),
                sage_inv.get("sfec_signature", ""),
                sage_inv.get("sfec_qr_code", ""),
                existing["id"]
            ))
            return existing["id"]
        else:
            statut_map = {
                "SAISI": "brouillon",
                "A COMPTABILISER": "a_comptabiliser",
                "COMPTABILISE": "valide",
            }
            statut = statut_map.get(sage_inv.get("statut", ""), "brouillon")

            cur.execute("""
                INSERT INTO invoices (
                    numero, date_facture, tiers_code, reference,
                    montant_ht, montant_ttc, montant_restant,
                    statut, source, sfec_statut, sfec_num_certif,
                    sfec_signature, sfec_qr_code, synced_sage,
                    sage_domaine, sage_type, sage_piece
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'sage', ?, ?, ?, ?, 1, 0, 6, ?)
            """, (
                numero, sage_inv.get("date_facture", ""),
                sage_inv.get("tiers_code", ""), sage_inv.get("reference", ""),
                sage_inv.get("montant_ht", 0), sage_inv.get("montant_ttc", 0),
                sage_inv.get("montant_restant", 0), statut,
                sage_inv.get("sfec_statut", ""), sage_inv.get("sfec_num_certif", ""),
                sage_inv.get("sfec_signature", ""), sage_inv.get("sfec_qr_code", ""),
                numero
            ))
            invoice_id = cur.lastrowid
            log_sync("invoices", invoice_id, numero, "sync_from_sage", "sage_to_sqlite", status="ok")
            return invoice_id
