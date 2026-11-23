"""Requêtes références (taux TVA, plan comptable) vers Sage 100."""
import logging

from app.integration.sage.connection import get_cursor
from app.integration.sage.schema import (
    _col_expr, get_table_map, list_all_tables,
)
from app.integration.sage.sql import quote_ident

logger = logging.getLogger("t-connector.db")


def fetch_tax_rates():
    t = get_table_map()
    if not t["tax_rate"]:
        logger.warning("Table TVA introuvable")
        return []

    try:
        with get_cursor() as cur:
            cur.execute("""
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = ?
            """, t["tax_rate"])
            tva_cols = {row[0].upper() for row in cur.fetchall()}

            p = "TA_" if "TA_CODE" in tva_cols else "TVA_"

            intitule_select = "ISNULL({}Intitule, '') AS libelle".format(p) if "{}INTITULE".format(p) in tva_cols else "'' AS libelle"
            taux_select = "ISNULL({}Taux, 0) AS taux".format(p) if "{}TAUX".format(p) in tva_cols else "0 AS taux"
            where_inactif = "WHERE {}Inactif = 0".format(p) if "{}INACTIF".format(p) in tva_cols else ""

            cur.execute("""
                SELECT
                    {p}Code AS id,
                    {p}Code AS code,
                    {intitule},
                    {taux},
                    1 AS est_actif
                FROM {tbl}
                {where}
                ORDER BY {p}Code
            """.format(p=p, tbl=quote_ident(t["tax_rate"]), intitule=intitule_select,
               taux=taux_select, where=where_inactif))
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, [str(v) if v is not None else "" for v in row])) for row in cur.fetchall()]
    except Exception as e:
        logger.error("Erreur requete taux TVA: %s", e)
        return []

def fetch_ledger_accounts():
    t = get_table_map()
    if not t["chart_of_accounts"]:
        logger.warning("Table plan comptable introuvable")
        return []

    try:
        with get_cursor() as cur:
            cur.execute("""
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = ?
            """, t["chart_of_accounts"])
            coa_cols = {row[0].upper() for row in cur.fetchall()}

            classe_select = "ISNULL(CG_Classe, '') AS type_compte" if "CG_CLASSE" in coa_cols else "'' AS type_compte"
            collectif_select = "CASE WHEN CG_Collectif = 1 THEN 1 ELSE 0 END AS est_compte_collectif" if "CG_COLLECTIF" in coa_cols else "0 AS est_compte_collectif"

            cur.execute("""
                SELECT
                    CG_Num AS id,
                    CG_Num AS code,
                    ISNULL(CG_Intitule, '') AS libelle,
                    {classe},
                    {collectif}
                FROM {tbl}
                ORDER BY CG_Num
            """.format(tbl=quote_ident(t["chart_of_accounts"]), classe=classe_select,
               collectif=collectif_select))
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, [str(v) if v is not None else "" for v in row])) for row in cur.fetchall()]
    except Exception as e:
        logger.error("Erreur requete plan comptable: %s", e)
        return []
