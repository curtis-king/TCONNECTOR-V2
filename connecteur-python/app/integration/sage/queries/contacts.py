"""Requêtes tiers (contacts) vers Sage 100."""
import logging

from app.integration.sage.connection import get_cursor
from app.integration.sage.schema import (
    _TABLE_COLUMNS, _col_expr, get_table_map,
)
from app.integration.sage.sql import quote_ident

logger = logging.getLogger("t-connector.db")


def fetch_contacts(type_filter=None):
    t = get_table_map()
    if not t["third_party"]:
        logger.warning("Table tiers introuvable")
        return []

    tp_cols = _TABLE_COLUMNS.get(t["third_party"].upper(), set())

    where = ""
    params = []
    has_type = "CT_TYPE" in tp_cols
    if has_type:
        if type_filter == "client":
            where = "WHERE c.CT_Type = 0"
        elif type_filter == "fournisseur":
            where = "WHERE c.CT_Type = 1"

    email_select = "ISNULL(c.CT_EMail, '') AS email" if "CT_EMAIL" in tp_cols else "'' AS email"
    phone_select = "ISNULL(c.CT_Telephone, '') AS telephone" if "CT_TELEPHONE" in tp_cols else "'' AS telephone"
    niu_select = ("ISNULL(c.CT_NIU, '') AS numero_fiscal" if "CT_NIU" in tp_cols else
                  "ISNULL(c.CT_Identifiant, '') AS numero_fiscal" if "CT_IDENTIFIANT" in tp_cols else
                  "'' AS numero_fiscal")

    if "CBCREATION" in tp_cols:
        create_select = "ISNULL(TRY_CONVERT(VARCHAR(30), c.cbCreation, 126), '') AS date_creation"
    else:
        create_select = "'' AS date_creation"

    if "CBMODIFICATION" in tp_cols:
        modif_select = "ISNULL(TRY_CONVERT(VARCHAR(30), c.cbModification, 126), '') AS date_modification"
    else:
        modif_select = "'' AS date_modification"

    if has_type:
        type_select = """CASE c.CT_Type
                            WHEN 0 THEN 'Client'
                            WHEN 1 THEN 'Fournisseur'
                            WHEN 2 THEN 'Les deux'
                            ELSE 'Inconnu'
                        END AS type_tiers"""
        client_flag = "CASE WHEN c.CT_Type IN (0, 2) THEN 1 ELSE 0 END AS est_client"
        fourni_flag = "CASE WHEN c.CT_Type IN (1, 2) THEN 1 ELSE 0 END AS est_fournisseur"
    else:
        type_select = "'' AS type_tiers"
        client_flag = "1 AS est_client"
        fourni_flag = "1 AS est_fournisseur"

    intitule_available = "CT_INTITULE" in tp_cols
    if intitule_available:
        name_select = "ISNULL(c.CT_Intitule, '') AS nom"
        order_by = "ORDER BY c.CT_Intitule"
    else:
        name_select = "'' AS nom"
        order_by = "ORDER BY c.CT_Num"

    try:
        with get_cursor() as cur:
            cur.execute("""
                SELECT
                    c.CT_Num AS id,
                    c.CT_Num AS code,
                    {nom},
                    {type},
                    {client_flag},
                    {fourni_flag},
                    {email},
                    {phone},
                    {niu},
                    {create},
                    {modif}
                FROM {tbl} c
                {where}
                {order}
            """.format(
                nom=name_select, type=type_select, client_flag=client_flag, fourni_flag=fourni_flag,
                email=email_select, phone=phone_select,
                niu=niu_select, create=create_select, modif=modif_select,
                tbl=quote_ident(t["third_party"]), where=where, order=order_by
            ), params)
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, [str(v) if v is not None else "" for v in row])) for row in cur.fetchall()]
    except Exception as e:
        logger.error("Erreur requete contacts: %s", e)
        return []

def fetch_contact_by_code(code):
    """Retourne un tiers Sage par son code, sans charger toute la table."""
    code = str(code or "").strip()
    if not code:
        return {}
    t = get_table_map()
    table = t.get("third_party", "")
    if not table:
        return {}

    cols = _TABLE_COLUMNS.get(table.upper(), set())
    name_col = next(
        (col for col in ("CT_Intitule", "CT_IntitulePayeur", "CT_RaisonSociale",
                         "CT_Libelle") if col.upper() in cols),
        None,
    )
    name_expr = "ISNULL(c.{}, '') AS nom".format(name_col) if name_col else "'' AS nom"
    addr_expr = ("ISNULL(c.CT_Adresse, '') AS adresse"
                 if "CT_ADRESSE" in cols else "'' AS adresse")
    phone_expr = ("ISNULL(c.CT_Telephone, '') AS telephone"
                  if "CT_TELEPHONE" in cols else "'' AS telephone")
    email_expr = ("ISNULL(c.CT_EMail, '') AS email"
                  if "CT_EMAIL" in cols else "'' AS email")
    niu_col = ("CT_NIU" if "CT_NIU" in cols else
               "CT_Identifiant" if "CT_IDENTIFIANT" in cols else None)
    niu_expr = ("ISNULL(c.{}, '') AS numero_fiscal".format(niu_col)
                if niu_col else "'' AS numero_fiscal")

    try:
        with get_cursor() as cur:
            cur.execute("""
                SELECT TOP 1
                    c.CT_Num AS code,
                    {nom},
                    {adresse},
                    {telephone},
                    {email},
                    {niu}
                FROM {tbl} c
                WHERE c.CT_Num = ?
            """.format(
                nom=name_expr, adresse=addr_expr, telephone=phone_expr,
                email=email_expr, niu=niu_expr, tbl=quote_ident(table),
            ), (code,))
            row = cur.fetchone()
            if not row:
                return {}
            columns = [desc[0] for desc in cur.description]
            return {
                key: (str(value) if value is not None else "")
                for key, value in zip(columns, row)
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("Contact Sage introuvable pour %s: %s", code, e)
        return {}
