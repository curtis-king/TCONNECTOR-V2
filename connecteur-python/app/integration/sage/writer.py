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
    return {"vente_domaine": 0, "vente_type": 6, "avoir_type": 7, "achat_domaine": 1}


_CONTACT_TYPE_MAP = {"client": 0, "fournisseur": 1}


def _trunc(val, length):
    return str(val or "").strip()[:length]


def write_contact_to_sage(contact):
    """Cree le tiers dans Sage F_COMPTET (idempotent).

    Le trigger TG_INS_CPTAF_DOCENTETE (erreur 82019) exige que toute piece
    de vente reference un F_COMPTET existant avec CT_Type=0. Sans ce push
    prealable, toutes les ecritures de factures echouent.
    Colonnes minimales uniquement : CT_Raccourci/CT_NumPayeur/CG_NumPrinc/
    CO_No restent vides pour ne pas declencher les controles TG_INS_F_COMPTET.
    """
    code = _trunc(contact.get("code", ""), 17)
    if not code:
        return {"success": False, "error": "code contact vide"}
    nom = _trunc(contact.get("nom", "") or code, 69)
    ctype = _CONTACT_TYPE_MAP.get(str(contact.get("type", "client")).lower(), 0)

    cursor_ctx = _get_sage_cursor()
    if cursor_ctx is None:
        try:
            log_sync("sage_contact", contact.get("id", 0), code, "write", "sqlite_to_sage",
                     status="error", error="SQL Server indisponible")
        except Exception:
            pass
        return {"success": False, "error": "SQL Server indisponible"}

    try:
        with cursor_ctx as cur:
            cur.execute("SELECT CT_Num, CT_Type FROM F_COMPTET WHERE CT_Num = ?", code)
            existing = cur.fetchone()
            if existing:
                try:
                    existing_type = int(float(str(existing[1])))
                except (ValueError, TypeError):
                    existing_type = -1
                if existing_type != ctype:
                    msg = "CT_Num '{}' existe avec CT_Type={} (attendu {})".format(code, existing_type, ctype)
                    logger.warning(msg)
                    return {"success": False, "error": msg}
            else:
                cur.execute("""
                    INSERT INTO F_COMPTET (
                        CT_Num, CT_Intitule, CT_Type, CT_Identifiant,
                        CT_Telephone, CT_EMail, CT_Adresse, CT_Ville, CT_Pays
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    code, nom, ctype,
                    _trunc(contact.get("niu", ""), 25),
                    _trunc(contact.get("telephone", ""), 21),
                    _trunc(contact.get("email", ""), 69),
                    _trunc(contact.get("adresse", ""), 35),
                    _trunc(contact.get("ville", ""), 35),
                    _trunc(contact.get("pays", "") or "CG", 35),
                ))

        with get_sqlite_cursor() as cur:
            cur.execute("""
                UPDATE contacts SET synced_sage = 1, sage_ct_num = ?, updated_at = datetime('now')
                WHERE code = ?
            """, (code, code))

        log_sync("sage_contact", contact.get("id", 0), code, "write", "sqlite_to_sage", status="ok")
        logger.info("Contact ecrit dans Sage: %s (type=%d)", code, ctype)
        return {"success": True, "ct_num": code}
    except Exception as e:
        try:
            log_sync("sage_contact", contact.get("id", 0), code, "write", "sqlite_to_sage",
                     status="error", error=str(e)[:200])
        except Exception:
            pass
        logger.error("Ecriture contact Sage echouee pour %s: %s", code, e)
        return {"success": False, "error": str(e)[:200]}


def ensure_contact_in_sage(tiers_code):
    """Retourne le CT_Num Sage pour un code contact local (creation si besoin)."""
    code = str(tiers_code or "").strip()
    if not code:
        return {"success": False, "error": "tiers_code vide"}
    contact = None
    with get_sqlite_cursor() as cur:
        cur.execute("SELECT * FROM contacts WHERE code = ?", (code,))
        row = cur.fetchone()
        if row is None:
            return {"success": False, "error": "contact '{}' inconnu (table contacts)".format(code)}
        try:
            contact = {k: row[k] for k in row.keys()}
        except Exception:
            contact = dict(row)
        if contact.get("synced_sage") and (contact.get("sage_ct_num") or ""):
            return {"success": True, "ct_num": contact["sage_ct_num"]}
    result = write_contact_to_sage(contact)
    if result.get("success"):
        return {"success": True, "ct_num": result["ct_num"]}
    return result

def _code_taxe(taux):
    t = float(taux or 0)
    if abs(t-18) < 0.01: return 'C18'
    if abs(t-5)  < 0.01: return 'C05'
    if abs(t-0)  < 0.01: return 'C00'
    if abs(t-20) < 0.01: return 'C20'
    return 'C18' if t>10 else 'C00'


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

    is_avoir = invoice.get("type_doc") == "avoir"
    doc_type = dc["avoir_type"] if is_avoir else dc["vente_type"]
    sign = -1 if is_avoir else 1

    tiers_col = "DO_Tiers"
    sage_tiers = ensure_contact_in_sage(invoice.get("tiers_code", ""))
    if not sage_tiers.get("success"):
        err = sage_tiers.get("error", "tiers non resolu")
        log_sync("sage_invoice", invoice["id"], numero, "write", "sqlite_to_sage",
                 status="error", error=err[:200])
        logger.error("Ecriture Sage annulee pour %s: %s", numero, err)
        return {"success": False, "error": err[:200]}
    tiers_val = sage_tiers["ct_num"]

    taux_entete = (invoice.get("lignes") or [{}])[0].get("taux_tva", 18)
    code_entete = _code_taxe(taux_entete)

    try:
        with cursor_ctx as cur:
            sql = """
                INSERT INTO F_DOCENTETE (
                    DO_Domaine, DO_Type, DO_Piece, DO_Date,
                    {tiers_col}, DO_Ref, DO_TotalHT, DO_CodeTaxe1, DO_Taxe1,
                    DO_TotalTTC, DO_NetAPayer, DO_Statut, DO_Valide
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """.format(tiers_col=tiers_col)

            cur.execute(sql, (
                dc["vente_domaine"], doc_type, numero, date_val,
                tiers_val, invoice.get("reference", ""),
                sign * invoice.get("montant_ht", 0),
                code_entete,
                sign * invoice.get("montant_tva", 0),
                sign * invoice.get("montant_ttc", 0),
                sign * invoice.get("montant_ttc", 0),
                2, 1
            ))

            lignes = invoice.get("lignes", [])
            for ligne in lignes:
                cur.execute("""
                    INSERT INTO F_DOCLIGNE (
                        DO_Domaine, DO_Type, DO_Piece, DL_Ligne,
                        DL_Design, DL_Qte, DL_PrixUnitaire, DL_MontantHT,
                        DL_CodeTaxe1, DL_Taxe1, DL_MontantTTC, CO_No, AR_Ref
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    dc["vente_domaine"], doc_type, numero,
                    ligne.get("numero_ligne", 1),
                    ligne.get("designation", ""),
                    sign * ligne.get("quantite", 1),
                    ligne.get("prix_unitaire", 0),
                    sign * ligne.get("montant_ht", 0),
                    _code_taxe(ligne.get("taux_tva", 18)),
                    ligne.get("taux_tva", 18),
                    sign * ligne.get("montant_ttc", 0),
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
            # I3 : détection dynamique des colonnes SFEC_* — la migration
            # n'a jamais été lancée sur BIJOU, donc on ne peut pas les
            # coder en dur sous peine de faire planter toute la lecture.
            cur.execute("""
                SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = 'F_DOCENTETE' AND COLUMN_NAME LIKE 'SFEC%'
            """)
            sfec_cols_present = {row[0] for row in cur.fetchall()}

            sfec_fields = ["SFEC_STATUT", "SFEC_NUM_CERTIF", "SFEC_SIGNATURE", "SFEC_QR_CODE"]
            sfec_select = ",\n                    ".join(
                f"ISNULL(d.{col}, '') AS {col.lower()}" if col in sfec_cols_present
                else f"'' AS {col.lower()}"
                for col in sfec_fields
            )

            # I2 : DO_Type IN (6,7) pour lire aussi les avoirs.
            # I1 : DO_Ref n'est JAMAIS utilisé comme lien avoir->facture,
            # c'est une référence externe (n° commande). reference_invoice_id
            # reste vide et doit être complété manuellement côté connecteur.
            query = f"""
                SELECT
                    CAST(d.DO_Domaine AS VARCHAR) + '-' + CAST(d.DO_Type AS VARCHAR) + '-' + d.DO_Piece AS id,
                    d.DO_Piece AS numero,
                    d.DO_Domaine AS sage_domaine,
                    d.DO_Type AS sage_type,
                    d.DO_Piece AS sage_piece,
                    CASE WHEN d.DO_Type = 7 THEN 'avoir' ELSE 'facture' END AS type_doc,
                    '' AS reference_invoice_id,
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
                    {sfec_select}
                FROM F_DOCENTETE d
                WHERE d.DO_Domaine = 0 AND d.DO_Type IN (6, 7)
                ORDER BY d.DO_Date DESC
            """
            cur.execute(query)

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
                try:
                    inv["sage_type"] = int(float(str(inv["sage_type"])))
                except (ValueError, TypeError):
                    inv["sage_type"] = 0
                invoices.append(inv)

            logger.info(
                "Factures/avoirs Sage lus: %d (SFEC cols detectees: %s)",
                len(invoices), sorted(sfec_cols_present) or "aucune"
            )
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
