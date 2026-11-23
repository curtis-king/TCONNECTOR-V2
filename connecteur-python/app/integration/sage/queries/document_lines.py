"""Requêtes lignes de documents (F_DOCLIGNE) vers Sage 100."""
import logging
from contextlib import nullcontext

from app.integration.sage.connection import get_cursor
from app.integration.sage.schema import (
    _TABLE_COLUMNS, _col_expr, _has_col, get_table_map,
)
from app.integration.sage.sql import quote_ident
from app.integration.sage.values import _safe_float

logger = logging.getLogger("t-connector.db")


def _doc_lines_query_parts():
    """Fragments SELECT/JOIN communs a fetch_doc_lines et fetch_doc_lines_batch.

    Champs d'affichage facture (modele standard Sage) : unite de gestion,
    nombre de colis (2nde quantite), conditionnement (libelle de la colonne
    article) et poids de ligne. Toute colonne absente de la base est
    remplacee par un fallback vide/zero : la lecture des lignes ne doit
    jamais echouer pour un champ d'affichage.
    """
    t = get_table_map()
    line_tbl = t.get("document_lines", "")
    line_cols = _TABLE_COLUMNS.get(line_tbl.upper(), set()) if line_tbl else set()
    art_tbl = t.get("article", "")
    art_cols = _TABLE_COLUMNS.get(art_tbl.upper(), set()) if art_tbl else set()

    joins = []
    if art_tbl:
        joins.append("LEFT JOIN {} a ON a.AR_Ref = l.AR_Ref".format(
            quote_ident(art_tbl)))
        article_cols = """
            , ISNULL(a.AR_Design, '') AS article_design
            , ISNULL(a.FA_CodeFamille, '') AS article_famille
            , ISNULL(a.AR_Type, 0) AS article_type
            , ISNULL(a.AR_Nature, 0) AS article_nature
        """
    else:
        article_cols = """
            , '' AS article_design
            , '' AS article_famille
            , 0 AS article_type
            , 0 AS article_nature
        """

    extra_cols = ""

    # Unite de gestion de l'article (colonne "Unite" du modele d'impression)
    if "UT_CODE" in art_cols:
        extra_cols += ", ISNULL(a.UT_Code, '') AS article_unit"
    else:
        extra_cols += ", '' AS article_unit"

    # Nombre de colis/conditionnements (colonne "Nb." du modele) : la 2e
    # quantite Sage est renseignee si l'article a un 2e conditionnement.
    qte2 = next((c for c in ("DL_Qte2", "DL_QTE2") if c.upper() in line_cols), None)
    if qte2:
        extra_cols += ", ISNULL(l.{}, 0) AS nb_colis".format(qte2)
    else:
        extra_cols += ", 0 AS nb_colis"

    # Conditionnement (colonne "Cond." du modele) : libelle de la colonne
    # article rattachee a la ligne via CO_No ("Fle de 2m", "Colis de 100u"...).
    cond_tbl = t.get("colonne", "")
    cond_cols = _TABLE_COLUMNS.get(cond_tbl.upper(), set()) if cond_tbl else set()
    if cond_tbl and "CO_LIBELLE" in cond_cols and "CO_NO" in line_cols:
        joins.append("LEFT JOIN {} cond ON cond.CO_No = l.CO_No".format(
            quote_ident(cond_tbl)))
        extra_cols += ", ISNULL(cond.CO_Libelle, '') AS conditionnement"
    else:
        extra_cols += ", '' AS conditionnement"

    # Poids de la ligne (colonne "Pds (Kg)" du modele) : net de ligne, sinon
    # brut de ligne, sinon poids net article x quantite.
    poids_col = next(
        (c for c in ("DL_PdsNet", "DL_PoidsNet", "DL_PdsBrut", "DL_PoidsBrut")
         if c.upper() in line_cols),
        None,
    )
    if poids_col:
        extra_cols += ", ISNULL(l.{}, 0) AS poids_kg".format(poids_col)
    elif art_tbl and "AR_POIDSNET" in art_cols:
        extra_cols += ", ISNULL(a.AR_PoidsNet, 0) * ISNULL(l.DL_Qte, 0) AS poids_kg"
    else:
        extra_cols += ", 0 AS poids_kg"

    # Codes et taux de taxe de la ligne. Les montants de taxes ne sont pas
    # stockes par Sage : ils sont recalcules. On lit les codes (1, 202, 203
    # chez le client) et les taux supplementaires pour identifier le role
    # de chaque taxe (TVA, centime, taxe supplementaire).
    tax_cols = ""
    for _index in (1, 2, 3):
        _code_col = "DL_CODETAXE{}".format(_index)
        _rate_col = "DL_TAXE{}".format(_index)
        if _code_col in line_cols:
            tax_cols += ", ISNULL(l.{}, '') AS code_taxe{}".format(
                _code_col, _index)
        if _index > 1 and _rate_col in line_cols:
            tax_cols += ", ISNULL(l.{}, 0) AS taux_taxe{}".format(
                _rate_col, _index)

    return {
        "join_clause": "\n                ".join(joins),
        "article_cols": article_cols,
        "extra_cols": extra_cols,
        "tax_cols": tax_cols,
    }

_LINE_NUMERIC_KEYS = (
    "quantite", "prix_unitaire", "montant_ht", "montant_tva", "montant_ttc",
    "taux_tva", "nb_colis", "poids_kg",
)

def _parse_doc_line(row_dict):
    for key in _LINE_NUMERIC_KEYS:
        if key in row_dict:
            try:
                row_dict[key] = round(float(str(row_dict[key] or "0").replace(",", ".")), 2)
            except (ValueError, TypeError):
                row_dict[key] = 0.0 if key != "taux_tva" else 18.0
    return row_dict

def fetch_doc_lines(domaine, type_doc, piece):
    t = get_table_map()
    if not t["document_lines"]:
        return []

    parts = _doc_lines_query_parts()

    try:
        with get_cursor() as cur:
            cur.execute("""
                SELECT
                    CAST(? AS VARCHAR) + '-' + CAST(? AS VARCHAR) + '-' + l.DO_Piece + '-' + CAST(l.DL_Ligne AS VARCHAR) AS id,
                    l.DL_Ligne AS numero_ligne,
                    ISNULL(l.DL_Design, '') AS description,
                    ISNULL(l.DL_Qte, 1) AS quantite,
                    ISNULL(l.DL_PrixUnitaire, 0) AS prix_unitaire,
                    ISNULL(l.DL_MontantHT, 0) AS montant_ht,
                    ISNULL(ROUND(l.DL_MontantHT * l.DL_Taxe1 / 100, 2), 0) AS montant_tva,
                    ISNULL(l.DL_MontantTTC, 0) AS montant_ttc,
                    ISNULL(l.DL_Taxe1, 0) AS taux_tva,
                    ISNULL(CAST(l.CO_No AS NVARCHAR), '') AS code_compte,
                    ISNULL(l.AR_Ref, '') AS code_produit
                    {article_cols}
                    {extra_cols}
                FROM {tbl} l
                {join}
                WHERE l.DO_Domaine = ?
                  AND l.DO_Type = ?
                  AND l.DO_Piece = ?
                ORDER BY l.DL_Ligne
            """.format(tbl=quote_ident(t["document_lines"]), article_cols=parts["article_cols"],
                       extra_cols=parts["extra_cols"], join=parts["join_clause"]),
                domaine, type_doc, domaine, type_doc, piece)
            columns = [desc[0] for desc in cur.description]
            result = []
            for row in cur.fetchall():
                line = dict(zip(columns, [str(v) if v is not None else "" for v in row]))
                result.append(_parse_doc_line(line))
            return result
    except Exception:
        return []

def fetch_doc_lines_batch(domaine, keys, chunk_size=200, cursor=None):
    """Lignes de N pieces en 1 requete par chunk (anti N+1).

    keys : liste de (do_type, numero). Retour : {(do_type, numero): [lignes]}.
    Meme SELECT que fetch_doc_lines. Chunks de 200 cles (600 params < limite 2100 pyodbc).

    `cursor` permet de reutiliser un curseur Sage deja ouvert. C'est
    necessaire dans read_invoices_from_sage() pour eviter une connexion
    imbriquee (deadlock avec le repli mono-connexion).
    """
    t = get_table_map()
    if not t["document_lines"] or not keys:
        return {}

    parts = _doc_lines_query_parts()

    out = {}
    for i in range(0, len(keys), chunk_size):
        chunk = keys[i:i + chunk_size]
        conds = " OR ".join(["(l.DO_Domaine = ? AND l.DO_Type = ? AND l.DO_Piece = ?)"] * len(chunk))
        params = []
        for dt, piece in chunk:
            params.extend([domaine, dt, piece])
        try:
            ctx = nullcontext(cursor) if cursor is not None else get_cursor()
            with ctx as cur:
                cur.execute("""
                    SELECT
                        CAST(l.DO_Domaine AS VARCHAR) + '-' + CAST(l.DO_Type AS VARCHAR) + '-' + l.DO_Piece + '-' + CAST(l.DL_Ligne AS VARCHAR) AS id,
                        l.DL_Ligne AS numero_ligne,
                        ISNULL(l.DL_Design, '') AS description,
                        ISNULL(l.DL_Qte, 1) AS quantite,
                        ISNULL(l.DL_PrixUnitaire, 0) AS prix_unitaire,
                        ISNULL(l.DL_MontantHT, 0) AS montant_ht,
                        ISNULL(ROUND(l.DL_MontantHT * l.DL_Taxe1 / 100, 2), 0) AS montant_tva,
                        ISNULL(l.DL_MontantTTC, 0) AS montant_ttc,
                        ISNULL(l.DL_Taxe1, 0) AS taux_tva,
                        ISNULL(CAST(l.CO_No AS NVARCHAR), '') AS code_compte,
                        ISNULL(l.AR_Ref, '') AS code_produit
                        {article_cols}
                        {extra_cols}
                        , l.DO_Type AS _dt, l.DO_Piece AS _piece
                    FROM {tbl} l
                    {join}
                    WHERE {conds}
                    ORDER BY l.DO_Piece, l.DL_Ligne
                """.format(tbl=quote_ident(t["document_lines"]), article_cols=parts["article_cols"],
                           extra_cols=parts["extra_cols"], join=parts["join_clause"],
                           conds=conds), params)
                columns = [desc[0] for desc in cur.description]
                idx = {c: j for j, c in enumerate(columns)}
                for row in cur.fetchall():
                    line = dict(zip(columns, [str(v) if v is not None else "" for v in row]))
                    _parse_doc_line(line)
                    out.setdefault((int(_safe_float(row[idx["_dt"]])), str(row[idx["_piece"]])), []).append(line)
        except Exception:
            continue
    return out
