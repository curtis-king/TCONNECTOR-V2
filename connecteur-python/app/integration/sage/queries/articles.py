"""Requêtes articles vers Sage 100."""
import logging

from app.integration.sage.connection import get_cursor
from app.integration.sage.schema import _col_expr, get_table_map
from app.integration.sage.sql import quote_ident
from app.integration.sage.values import _safe_float

logger = logging.getLogger("t-connector.db")


def fetch_articles(limit=None):
    t = get_table_map()
    article_tbl = t.get("article", "")
    if not article_tbl:
        logger.error("Table F_ARTICLE introuvable")
        return []

    try:
        with get_cursor() as cur:
            cur.execute("""
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = ?
            """, article_tbl)
            cols = {row[0].upper() for row in cur.fetchall()}
    except Exception as e:
        logger.error("Erreur lecture colonnes F_ARTICLE: %s", e)
        return []

    def _col(keys, default, cast=False):
        for k in keys:
            if k in cols:
                expr = "a.{}".format(k)
                if cast:
                    expr = "CAST(a.{} AS VARCHAR)".format(k)
                return "ISNULL({}, {})".format(expr, default)
        return default

    ref_s = _col(("AR_REF",), "''")
    design_s = _col(("AR_DESIGN", "AR_LIBELLE", "AR_DESIGN2"), "''")
    famille_s = _col(("FA_CODEFAMILLE",), "''")
    nature_s = _col(("AR_NATURE",), "0")
    type_s = _col(("AR_TYPE",), "0")
    prix_vente_s = _col(("AR_PRIXVEN", "AR_PRIXVENTE", "AR_PRIXVENNOUV", "AR_PRIXTTC"), "0")
    prix_achat_s = _col(("AR_PRIXACH", "AR_PRIXACHAT", "AR_PRIXACHNOUV"), "0")
    unite_s = _col(("AR_UNITEVEN", "AR_UNITE", "AR_UNITVENTE"), "'U'", cast=True)
    inactif_s = _col(("AR_INACTIF",), "0")
    barcode_s = _col(("AR_CODEBARRE",), "''")
    tva_s = _col(("AR_CODETVA", "FA_CODETVA"), "'18'", cast=True)

    limit_clause = "TOP {}".format(limit) if limit else ""
    query = """
        SELECT {limit}
            {ref} AS ref,
            {design} AS designation,
            {famille} AS famille,
            {nature} AS nature,
            {type} AS article_type,
            {prix_vente} AS prix_vente,
            {prix_achat} AS prix_achat,
            0 AS stock,
            {unite} AS unite,
            {inactif} AS inactif,
            {barcode} AS barcode,
            {tva} AS tva_code
        FROM {tbl} a
        ORDER BY a.AR_Ref
    """.format(
        limit=limit_clause, ref=ref_s, design=design_s, famille=famille_s,
        nature=nature_s, type=type_s, prix_vente=prix_vente_s,
        prix_achat=prix_achat_s, unite=unite_s,
        inactif=inactif_s, barcode=barcode_s, tva=tva_s,
        tbl=quote_ident(article_tbl)
    )

    try:
        with get_cursor() as cur:
            cur.execute(query)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()

        articles = []
        for row in rows:
            art = dict(zip(columns, [str(v) if v is not None else "" for v in row]))
            for key in ("nature", "article_type", "prix_vente", "prix_achat", "stock", "inactif"):
                art[key] = _safe_float(art.get(key, 0))
            try:
                est_actif = 1 if int(art.get("inactif", 0)) == 0 else 0
            except (ValueError, TypeError):
                est_actif = 1
            art["est_actif"] = est_actif
            articles.append(art)

        _merge_article_stock(articles, article_tbl)

        logger.info("Articles Sage charges: %d", len(articles))
        return articles
    except Exception as e:
        logger.error("Erreur requete articles F_ARTICLE: %s", e)
        return []

def _merge_article_stock(articles, article_tbl):
    if not articles:
        return
    try:
        with get_cursor() as cur:
            cur.execute("""
                SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ?
            """, "F_ARTSTOCK")
            stock_cols = {row[0].upper() for row in cur.fetchall()}
    except Exception:
        return
    if "AR_REF" not in stock_cols or "AS_QTESTO" not in stock_cols:
        return
    try:
        with get_cursor() as cur:
            cur.execute("""
                SELECT RTRIM(LTRIM(AR_Ref)) AS ar_ref, SUM(AS_QteSto) AS qte
                FROM F_ARTSTOCK
                GROUP BY AR_Ref
            """)
            by_ref = {}
            for art in articles:
                _k = str(art.get("ref", "")).strip()
                if _k and _k not in by_ref:
                    by_ref[_k] = art  # comme break : premiere occurrence gagne
            for row in cur.fetchall():
                ref = str(row[0]).strip() if row[0] is not None else ""
                art = by_ref.get(ref)
                if art is not None:
                    try:
                        art["stock"] = _safe_float(row[1])
                    except (ValueError, TypeError):
                        art["stock"] = 0.0
    except Exception as e:
        logger.warning("Stock articles F_ARTSTOCK indisponible: %s", e)
