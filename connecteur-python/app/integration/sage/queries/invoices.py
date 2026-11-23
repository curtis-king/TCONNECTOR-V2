"""Requêtes factures (ventes/achats), lignes et certifications vers Sage 100."""
import logging
from contextlib import nullcontext
from datetime import datetime

from app.integration.sage.connection import get_cursor
from app.integration.sage.queries.document_lines import (
    fetch_doc_lines, fetch_doc_lines_batch,
)
from app.integration.sage.schema import (
    _TABLE_COLUMNS, _col_expr, _detect_sfec_columns, _get_tiers_col,
    _has_col, _refresh_sfec_columns, _tiers_name_expr, credit_doc_types,
    get_domain_config, get_table_map, sales_doc_types,
)
from app.integration.sage.sql import quote_ident
from app.integration.sage.values import _map_devise, _safe_float

logger = logging.getLogger("t-connector.db")

def fetch_sales_invoices(updated_from=None, limit=None):
    _refresh_sfec_columns()
    t = get_table_map()
    dc = get_domain_config()

    if not t["documents"]:
        logger.error("Table F_DOCENTETE introuvable")
        return []

    has_sfec = _detect_sfec_columns(t["documents"])

    sfec_select = ""
    if has_sfec:
        sfec_select = """,
            ISNULL(d.SFEC_NUM_CERTIF, '') AS sfec_num_certif,
            ISNULL(TRY_CONVERT(VARCHAR(30), d.SFEC_DATE_CERTIF, 126), '') AS sfec_date_certif,
            ISNULL(d.SFEC_STATUT, '') AS sfec_statut"""
    else:
        sfec_select = """,
            '' AS sfec_num_certif,
            '' AS sfec_date_certif,
            '' AS sfec_statut"""

    limit_clause = "TOP {}".format(limit) if limit else "TOP 1000"
    doc_types = sales_doc_types(dc)
    credit_types = credit_doc_types(dc)
    types_in = ", ".join(str(t) for t in doc_types)
    credit_in = ", ".join(str(t) for t in credit_types)
    where = "WHERE d.DO_Domaine = {} AND d.DO_Type IN ({})".format(
        dc["vente_domain"], types_in
    )
    type_doc_case = (
        "CASE WHEN d.DO_Type IN ({}) THEN 'avoir' ELSE 'vente' END"
        .format(credit_in)
    )

    tiers_col = _get_tiers_col()
    has_third_party = bool(t.get("third_party"))
    has_third_party_cols = bool(_TABLE_COLUMNS.get(t.get("third_party", "").upper(), set()))
    tp_cols = _TABLE_COLUMNS.get(t.get("third_party", "").upper(), set()) if has_third_party else set()

    if tiers_col and has_third_party and has_third_party_cols and "CT_NUM" in tp_cols:
        tiers_select = "d.{} AS code_tiers".format(tiers_col)
        nom_tiers_select = _tiers_name_expr()
        niu_select = ("ISNULL(c.CT_NIU, '') AS recipient_niu" if "CT_NIU" in tp_cols else
                      "ISNULL(c.CT_Identifiant, '') AS recipient_niu" if "CT_IDENTIFIANT" in tp_cols else
                      "'' AS recipient_niu")
        phone_select = "ISNULL(c.CT_Telephone, '') AS recipient_phone" if "CT_TELEPHONE" in tp_cols else "'' AS recipient_phone"
        email_select = "ISNULL(c.CT_EMail, '') AS recipient_email" if "CT_EMAIL" in tp_cols else "'' AS recipient_email"
        addr_select = "ISNULL(c.CT_Adresse, '') AS recipient_address" if "CT_ADRESSE" in tp_cols else "'' AS recipient_address"
        classif_select = "ISNULL(c.CT_Statistique05, '') AS recipient_type_raw" if "CT_STATISTIQUE05" in tp_cols else "'' AS recipient_type_raw"
        join_clause = "LEFT JOIN {} c ON d.{} = c.CT_Num".format(
            quote_ident(t["third_party"]), tiers_col)
    elif tiers_col:
        tiers_select = "d.{} AS code_tiers".format(tiers_col)
        nom_tiers_select = "'' AS nom_tiers"
        niu_select = "'' AS recipient_niu"
        phone_select = "'' AS recipient_phone"
        email_select = "'' AS recipient_email"
        addr_select = "'' AS recipient_address"
        classif_select = "'' AS recipient_type_raw"
        join_clause = ""
    else:
        tiers_select = "'' AS code_tiers"
        nom_tiers_select = "'' AS nom_tiers"
        niu_select = "'' AS recipient_niu"
        phone_select = "'' AS recipient_phone"
        email_select = "'' AS recipient_email"
        addr_select = "'' AS recipient_address"
        classif_select = "'' AS recipient_type_raw"
        join_clause = ""

    doc_cols = _TABLE_COLUMNS.get(t["documents"].upper(), set())

    has_ttc = "DO_TOTALTTC" in doc_cols
    ttc_expr = "ISNULL(d.DO_TotalTTC, 0)" if has_ttc else "ISNULL(d.DO_TotalHT, 0) + ISNULL(d.DO_Taxe1, 0) + ISNULL(d.DO_Taxe2, 0) + ISNULL(d.DO_Taxe3, 0)"
    tva_expr = "ISNULL(d.DO_TotalTTC - d.DO_TotalHT, 0)" if has_ttc else "ISNULL(d.DO_Taxe1, 0) + ISNULL(d.DO_Taxe2, 0) + ISNULL(d.DO_Taxe3, 0)"

    ref_select = "ISNULL(d.DO_Ref, '') AS reference" if "DO_REF" in doc_cols else "'' AS reference"
    net_select = "ISNULL(d.DO_NetAPayer, {}) AS montant_restant".format(ttc_expr) if "DO_NETAPAYER" in doc_cols else "{} AS montant_restant".format(ttc_expr)
    valide_select = "ISNULL(d.DO_Valide, 0) AS valide" if "DO_VALIDE" in doc_cols else "0 AS valide"
    payment_select = "ISNULL(d.DO_ModeReglement, '') AS payment_method" if "DO_MODEREGLEMENT" in doc_cols else "'' AS payment_method"
    devise_select = "ISNULL(CAST(d.DO_Devise AS VARCHAR), '0') AS devise" if "DO_DEVISE" in doc_cols else "'0' AS devise"
    params = []
    if updated_from:
        try:
            dt = datetime.fromisoformat(str(updated_from).replace("Z", "+00:00"))
        except Exception:
            dt = updated_from
        if "CBMODIFICATION" in doc_cols:
            where += " AND (d.DO_Date >= ? OR d.cbModification >= ?)"
            params.extend([dt, dt])
        else:
            where += " AND d.DO_Date >= ?"
            params.append(dt)

    if "CBCREATION" in doc_cols:
        date_select = """ISNULL(TRY_CONVERT(VARCHAR(30), d.cbCreation, 126), '') AS date_creation,
            ISNULL(TRY_CONVERT(VARCHAR(30), d.cbModification, 126), '') AS date_modification"""
    elif "CBMODIFICATION" in doc_cols:
        date_select = """ISNULL(TRY_CONVERT(VARCHAR(30), d.cbModification, 126), '') AS date_creation,
            ISNULL(TRY_CONVERT(VARCHAR(30), d.cbModification, 126), '') AS date_modification"""
    else:
        date_select = "'' AS date_creation, '' AS date_modification"

    query = """
        SELECT {limit}
            CAST({vd} AS VARCHAR) + '-' + CAST(d.DO_Type AS VARCHAR) + '-' + d.DO_Piece AS id,
            d.DO_Piece AS numero,
            'sage' AS source,
            ISNULL(TRY_CONVERT(VARCHAR(10), d.DO_Date, 120), '') AS date_facture,
            ISNULL(TRY_CONVERT(VARCHAR(10), d.DO_Date, 120), '') AS date_echeance,
            {ref},
            {tiers},
            {nom_tiers},
            {niu},
            {phone},
            {email},
            {addr},
            {classif},
            ISNULL(d.DO_TotalHT, 0) AS montant_ht,
            {tva_expr} AS montant_tva,
            {ttc_expr} AS montant_ttc,
            {net},
            {payment}, {devise},
            d.DO_Statut AS statut_code,
            CASE
                WHEN d.DO_Statut = 0 THEN 'SAISI'
                WHEN d.DO_Statut = 1 THEN 'CONFIRME'
                WHEN d.DO_Statut = 2 THEN 'A COMPTABILISER'
                WHEN d.DO_Statut = 3 THEN 'COMPTABILISE'
                WHEN d.DO_Statut = 4 THEN 'LETTRÉ'
                ELSE 'SAISI'
            END AS statut,
            {valide},
            {type_doc_case},
            d.DO_Type AS do_type_raw,
            {dates}
            {sfec}
        FROM {tbl} d
        {join}
        {where}
        ORDER BY d.DO_Date DESC
    """.format(
        type_doc_case=type_doc_case,
        payment=payment_select,
        devise=devise_select,
        limit=limit_clause, vd=dc["vente_domain"],
        ref=ref_select, tiers=tiers_select, nom_tiers=nom_tiers_select,
        niu=niu_select, phone=phone_select, email=email_select, addr=addr_select,
        classif=classif_select,
        net=net_select, valide=valide_select, dates=date_select,
        sfec=sfec_select, ttc_expr=ttc_expr, tva_expr=tva_expr,
        tbl=quote_ident(t["documents"]), join=join_clause, where=where
    )

    try:
        with get_cursor() as cur:
            cur.execute(query, params)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()

        invoices = []
        idx = {c: i for i, c in enumerate(columns)}
        _keys = []
        for _r in rows:
            try:
                _dt = int(_safe_float(_r[idx["do_type_raw"]]))
            except (ValueError, TypeError, KeyError):
                _dt = dc["facture_type"]
            _keys.append((_dt, str(_r[idx["numero"]])))
        _lines_map = fetch_doc_lines_batch(dc["vente_domain"], _keys)
        for row in rows:
            inv = dict(zip(columns, [str(v) if v is not None else "" for v in row]))
            inv["tiers_code"] = inv.get("code_tiers", "")
            inv["montant_ht"] = round(_safe_float(row[idx["montant_ht"]]), 2)
            inv["montant_tva"] = round(_safe_float(row[idx["montant_tva"]]), 2)
            inv["montant_ttc"] = round(_safe_float(row[idx["montant_ttc"]]), 2)
            inv["montant_restant"] = round(_safe_float(row[idx["montant_restant"]]), 2)
            inv["devise"] = _map_devise(inv.get("devise", "0"))
            try:
                inv["statut_code"] = int(_safe_float(row[idx["statut_code"]]))
            except (ValueError, TypeError):
                inv["statut_code"] = 0
            try:
                inv["valide"] = int(_safe_float(row[idx["valide"]]))
            except (ValueError, TypeError):
                inv["valide"] = 0
            try:
                do_type_raw = int(_safe_float(row[idx["do_type_raw"]]))
            except (ValueError, TypeError):
                do_type_raw = dc["facture_type"]
            inv["lignes"] = _lines_map.get((do_type_raw, inv["numero"])) or fetch_doc_lines(dc["vente_domain"], do_type_raw, inv["numero"])
            invoices.append(inv)

        if invoices:
            logger.info("Factures ventes chargees: %d", len(invoices))
        else:
            logger.debug("Factures ventes chargees: 0")
        return invoices
    except Exception as e:
        logger.warning("Erreur requete factures ventes (requete principale): %s - tentative fallback", e)
        return _fetch_sales_invoices_fallback(dc, t)

def _fetch_sales_invoices_fallback(dc, t):
    doc_cols = _TABLE_COLUMNS.get(t.get("documents", "").upper(), set())
    has_ttc = "DO_TOTALTTC" in doc_cols
    ttc_expr = "ISNULL(d.DO_TotalTTC, 0)" if has_ttc else "ISNULL(d.DO_TotalHT, 0) + ISNULL(d.DO_Taxe1, 0) + ISNULL(d.DO_Taxe2, 0) + ISNULL(d.DO_Taxe3, 0)"
    tva_expr = "ISNULL(d.DO_TotalTTC - d.DO_TotalHT, 0)" if has_ttc else "ISNULL(d.DO_Taxe1, 0) + ISNULL(d.DO_Taxe2, 0) + ISNULL(d.DO_Taxe3, 0)"
    doc_types = sales_doc_types(dc)
    credit_types = credit_doc_types(dc)
    types_in = ", ".join(str(t) for t in doc_types)
    credit_in = ", ".join(str(t) for t in credit_types)

    fallback_query = """
        SELECT TOP 500
            CAST({vd} AS VARCHAR) + '-' + CAST(d.DO_Type AS VARCHAR) + '-' + d.DO_Piece AS id,
            d.DO_Piece AS numero,
            'sage' AS source,
            ISNULL(TRY_CONVERT(VARCHAR(10), d.DO_Date, 120), '') AS date_facture,
            ISNULL(TRY_CONVERT(VARCHAR(10), d.DO_Date, 120), '') AS date_echeance,
            ISNULL(d.DO_Ref, '') AS reference,
            '' AS code_tiers,
            '' AS nom_tiers,
            '' AS recipient_niu,
            '' AS recipient_phone,
            '' AS recipient_email,
            '' AS recipient_address,
            '' AS recipient_type_raw,
            ISNULL(d.DO_TotalHT, 0) AS montant_ht,
            {tva_expr} AS montant_tva,
            {ttc_expr} AS montant_ttc,
            {ttc_expr} AS montant_restant,
            ISNULL(d.DO_Statut, 0) AS statut_code,
            CASE
                WHEN d.DO_Statut = 0 THEN 'SAISI'
                WHEN d.DO_Statut = 1 THEN 'CONFIRME'
                WHEN d.DO_Statut = 2 THEN 'A COMPTABILISER'
                WHEN d.DO_Statut = 3 THEN 'COMPTABILISE'
                WHEN d.DO_Statut = 4 THEN 'LETTRÉ'
                ELSE 'SAISI'
            END AS statut,
            ISNULL(d.DO_Valide, 0) AS valide,
            CASE WHEN d.DO_Type IN ({credit_in}) THEN 'avoir' ELSE 'vente' END AS type_doc,
            d.DO_Type AS do_type_raw,
            '' AS date_creation,
            '' AS date_modification,
            '' AS sfec_num_certif,
            '' AS sfec_date_certif,
            '' AS sfec_statut
        FROM {tbl} d
        WHERE d.DO_Domaine = {vd} AND d.DO_Type IN ({types_in})
        ORDER BY d.DO_Piece DESC
    """.format(tbl=quote_ident(t["documents"]), vd=dc["vente_domain"], types_in=types_in,
               credit_in=credit_in, ttc_expr=ttc_expr, tva_expr=tva_expr)

    try:
        with get_cursor() as cur:
            cur.execute(fallback_query)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()

        invoices = []
        idx = {c: i for i, c in enumerate(columns)}
        _keys = []
        for _r in rows:
            try:
                _dt = int(_safe_float(_r[idx["do_type_raw"]]))
            except (ValueError, TypeError, KeyError):
                _dt = dc["facture_type"]
            _keys.append((_dt, str(_r[idx["numero"]])))
        _lines_map = fetch_doc_lines_batch(dc["vente_domain"], _keys)
        for row in rows:
            inv = dict(zip(columns, [str(v) if v is not None else "" for v in row]))
            inv["tiers_code"] = inv.get("code_tiers", "")
            inv["montant_ht"] = round(_safe_float(row[idx["montant_ht"]]), 2)
            inv["montant_tva"] = round(_safe_float(row[idx["montant_tva"]]), 2)
            inv["montant_ttc"] = round(_safe_float(row[idx["montant_ttc"]]), 2)
            inv["montant_restant"] = round(_safe_float(row[idx["montant_restant"]]), 2)
            try:
                inv["statut_code"] = int(_safe_float(row[idx["statut_code"]]))
            except (ValueError, TypeError):
                inv["statut_code"] = 0
            try:
                inv["valide"] = int(_safe_float(row[idx["valide"]]))
            except (ValueError, TypeError):
                inv["valide"] = 0
            try:
                do_type_raw = int(_safe_float(row[idx["do_type_raw"]]))
            except (ValueError, TypeError, KeyError):
                do_type_raw = dc["facture_type"]
            inv["lignes"] = _lines_map.get((do_type_raw, inv["numero"])) or fetch_doc_lines(dc["vente_domain"], do_type_raw, inv["numero"])
            invoices.append(inv)

        if invoices:
            logger.info("Factures ventes fallback chargees: %d", len(invoices))
        else:
            logger.debug("Factures ventes fallback chargees: 0")
        return invoices
    except Exception as e:
        logger.error("Erreur requete factures ventes (fallback): %s", e)
        return []

def fetch_purchase_invoices(updated_from=None, limit=None):
    _refresh_sfec_columns()
    t = get_table_map()
    dc = get_domain_config()

    if not t["documents"]:
        logger.error("Table F_DOCENTETE introuvable")
        return []

    if limit is None:
        limit = 1000
    limit_clause = "TOP {}".format(limit) if limit else ""
    where = "WHERE d.DO_Domaine = {}".format(dc["achat_domain"])

    params = []
    if updated_from:
        where += " AND d.DO_Date >= ?"
        try:
            dt = datetime.fromisoformat(str(updated_from).replace("Z", "+00:00"))
            params.append(dt)
        except Exception:
            params.append(updated_from)

    tiers_col = _get_tiers_col()
    has_third_party = bool(t.get("third_party"))
    has_third_party_cols = bool(_TABLE_COLUMNS.get(t.get("third_party", "").upper(), set()))
    tp_cols = _TABLE_COLUMNS.get(t.get("third_party", "").upper(), set()) if has_third_party else set()

    if tiers_col and has_third_party and has_third_party_cols and "CT_NUM" in tp_cols:
        tiers_select = "d.{} AS code_tiers".format(tiers_col)
        nom_tiers_select = _tiers_name_expr()
        niu_select = ("ISNULL(c.CT_NIU, '') AS recipient_niu" if "CT_NIU" in tp_cols else
                      "ISNULL(c.CT_Identifiant, '') AS recipient_niu" if "CT_IDENTIFIANT" in tp_cols else
                      "'' AS recipient_niu")
        phone_select = "ISNULL(c.CT_Telephone, '') AS recipient_phone" if "CT_TELEPHONE" in tp_cols else "'' AS recipient_phone"
        email_select = "ISNULL(c.CT_EMail, '') AS recipient_email" if "CT_EMAIL" in tp_cols else "'' AS recipient_email"
        addr_select = "ISNULL(c.CT_Adresse, '') AS recipient_address" if "CT_ADRESSE" in tp_cols else "'' AS recipient_address"
        classif_select = "ISNULL(c.CT_Statistique05, '') AS recipient_type_raw" if "CT_STATISTIQUE05" in tp_cols else "'' AS recipient_type_raw"
        join_clause = "LEFT JOIN {} c ON d.{} = c.CT_Num".format(
            quote_ident(t["third_party"]), tiers_col)
    elif tiers_col:
        tiers_select = "d.{} AS code_tiers".format(tiers_col)
        nom_tiers_select = "'' AS nom_tiers"
        niu_select = "'' AS recipient_niu"
        phone_select = "'' AS recipient_phone"
        email_select = "'' AS recipient_email"
        addr_select = "'' AS recipient_address"
        classif_select = "'' AS recipient_type_raw"
        join_clause = ""
    else:
        tiers_select = "'' AS code_tiers"
        nom_tiers_select = "'' AS nom_tiers"
        niu_select = "'' AS recipient_niu"
        phone_select = "'' AS recipient_phone"
        email_select = "'' AS recipient_email"
        addr_select = "'' AS recipient_address"
        classif_select = "'' AS recipient_type_raw"
        join_clause = ""

    doc_cols = _TABLE_COLUMNS.get(t["documents"].upper(), set())

    has_ttc = "DO_TOTALTTC" in doc_cols
    ttc_expr = "ISNULL(d.DO_TotalTTC, 0)" if has_ttc else "ISNULL(d.DO_TotalHT, 0) + ISNULL(d.DO_Taxe1, 0) + ISNULL(d.DO_Taxe2, 0) + ISNULL(d.DO_Taxe3, 0)"
    tva_expr = "ISNULL(d.DO_TotalTTC - d.DO_TotalHT, 0)" if has_ttc else "ISNULL(d.DO_Taxe1, 0) + ISNULL(d.DO_Taxe2, 0) + ISNULL(d.DO_Taxe3, 0)"

    payment_select = "ISNULL(d.DO_ModeReglement, '') AS payment_method" if "DO_MODEREGLEMENT" in doc_cols else "'' AS payment_method"
    devise_select = "ISNULL(CAST(d.DO_Devise AS VARCHAR), '0') AS devise" if "DO_DEVISE" in doc_cols else "'0' AS devise"
    ref_select = "ISNULL(d.DO_Ref, '') AS reference" if "DO_REF" in doc_cols else "'' AS reference"
    net_select = "ISNULL(d.DO_NetAPayer, {}) AS montant_restant".format(ttc_expr) if "DO_NETAPAYER" in doc_cols else "{} AS montant_restant".format(ttc_expr)

    if "CBMODIFICATION" in doc_cols:
        date_select = """ISNULL(TRY_CONVERT(VARCHAR(30), d.cbModification, 126), '') AS date_creation,
            ISNULL(TRY_CONVERT(VARCHAR(30), d.cbModification, 126), '') AS date_modification"""
    elif "CBCREATION" in doc_cols:
        date_select = """ISNULL(TRY_CONVERT(VARCHAR(30), d.cbCreation, 126), '') AS date_creation,
            ISNULL(TRY_CONVERT(VARCHAR(30), d.cbCreation, 126), '') AS date_modification"""
    else:
        date_select = "'' AS date_creation, '' AS date_modification"

    query = """
        SELECT {limit}
            CAST({ad} AS VARCHAR) + '-' + CAST(d.DO_Type AS VARCHAR) + '-' + d.DO_Piece AS id,
            d.DO_Piece AS numero,
            ISNULL(TRY_CONVERT(VARCHAR(10), d.DO_Date, 120), '') AS date_facture,
            ISNULL(TRY_CONVERT(VARCHAR(10), d.DO_Date, 120), '') AS date_echeance,
            {ref},
            {tiers},
            {nom_tiers},
            {niu},
            {phone},
            {email},
            {addr},
            {classif},
            CASE WHEN d.DO_Type = {at} THEN 'avoir' ELSE 'achat' END AS type_doc,
            d.DO_Type AS do_type_raw,
            {payment}, {devise},
            ISNULL(d.DO_TotalHT, 0) AS montant_ht,
            {tva_expr} AS montant_tva,
            {ttc_expr} AS montant_ttc,
            {net},
            d.DO_Statut AS statut_code,
            CASE
                WHEN d.DO_Statut = 0 THEN 'SAISI'
                WHEN d.DO_Statut = 1 THEN 'CONFIRME'
                WHEN d.DO_Statut = 2 THEN 'A COMPTABILISER'
                WHEN d.DO_Statut = 3 THEN 'COMPTABILISE'
                WHEN d.DO_Statut = 4 THEN 'LETTRÉ'
                ELSE 'SAISI'
            END AS statut,
            {dates}
        FROM {tbl} d
        {join}
        {where}
        ORDER BY d.DO_Date DESC
    """.format(
        limit=limit_clause, ad=dc["achat_domain"], at=dc["avoir_type"],
        payment=payment_select, devise=devise_select,
        ref=ref_select, tiers=tiers_select, nom_tiers=nom_tiers_select,
        niu=niu_select, phone=phone_select, email=email_select, addr=addr_select,
        classif=classif_select,
        net=net_select, dates=date_select, ttc_expr=ttc_expr, tva_expr=tva_expr,
        tbl=quote_ident(t["documents"]), join=join_clause, where=where
    )

    try:
        with get_cursor() as cur:
            cur.execute(query, params)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()

        invoices = []
        idx = {c: i for i, c in enumerate(columns)}
        _keys = []
        for _r in rows:
            try:
                _keys.append((int(_safe_float(_r[idx["do_type_raw"]])), str(_r[idx["numero"]])))
            except (ValueError, TypeError, KeyError):
                _keys.append((None, None))
        _lines_map = fetch_doc_lines_batch(dc["achat_domain"], [(a, b) for a, b in _keys if a is not None])
        for row in rows:
            inv = dict(zip(columns, [str(v) if v is not None else "" for v in row]))
            inv["tiers_code"] = inv.get("code_tiers", "")
            inv["montant_ht"] = round(_safe_float(row[idx["montant_ht"]]), 2)
            inv["montant_tva"] = round(_safe_float(row[idx["montant_tva"]]), 2)
            inv["montant_ttc"] = round(_safe_float(row[idx["montant_ttc"]]), 2)
            inv["montant_restant"] = round(_safe_float(row[idx["montant_restant"]]), 2)
            _dt = int(_safe_float(row[idx["do_type_raw"]]))
            inv["lignes"] = _lines_map.get((_dt, inv["numero"])) or fetch_doc_lines(dc["achat_domain"], _dt, inv["numero"])
            invoices.append(inv)

        if invoices:
            logger.info("Factures achats chargees: %d", len(invoices))
        else:
            logger.debug("Factures achats chargees: 0")
        return invoices
    except Exception as e:
        logger.warning("Erreur requete factures achats (requete principale): %s - tentative fallback", e)
        return _fetch_purchase_invoices_fallback(dc, t)

def _fetch_purchase_invoices_fallback(dc, t):
    doc_cols = _TABLE_COLUMNS.get(t.get("documents", "").upper(), set())
    has_ttc = "DO_TOTALTTC" in doc_cols
    ttc_expr = "ISNULL(d.DO_TotalTTC, 0)" if has_ttc else "ISNULL(d.DO_TotalHT, 0) + ISNULL(d.DO_Taxe1, 0) + ISNULL(d.DO_Taxe2, 0) + ISNULL(d.DO_Taxe3, 0)"
    tva_expr = "ISNULL(d.DO_TotalTTC - d.DO_TotalHT, 0)" if has_ttc else "ISNULL(d.DO_Taxe1, 0) + ISNULL(d.DO_Taxe2, 0) + ISNULL(d.DO_Taxe3, 0)"

    fallback_query = """
        SELECT TOP 500
            CAST({ad} AS VARCHAR) + '-' + CAST(d.DO_Type AS VARCHAR) + '-' + d.DO_Piece AS id,
            d.DO_Piece AS numero,
            ISNULL(TRY_CONVERT(VARCHAR(10), d.DO_Date, 120), '') AS date_facture,
            ISNULL(TRY_CONVERT(VARCHAR(10), d.DO_Date, 120), '') AS date_echeance,
            ISNULL(d.DO_Ref, '') AS reference,
            '' AS code_tiers,
            '' AS nom_tiers,
            '' AS recipient_niu,
            '' AS recipient_phone,
            '' AS recipient_email,
            '' AS recipient_address,
            '' AS recipient_type_raw,
            CASE WHEN d.DO_Type = {at} THEN 'avoir' ELSE 'achat' END AS type_doc,
            d.DO_Type AS do_type_raw,
            ISNULL(d.DO_TotalHT, 0) AS montant_ht,
            {tva_expr} AS montant_tva,
            {ttc_expr} AS montant_ttc,
            {ttc_expr} AS montant_restant,
            ISNULL(d.DO_Statut, 0) AS statut_code,
            CASE
                WHEN d.DO_Statut = 0 THEN 'SAISI'
                WHEN d.DO_Statut = 1 THEN 'CONFIRME'
                WHEN d.DO_Statut = 2 THEN 'A COMPTABILISER'
                WHEN d.DO_Statut = 3 THEN 'COMPTABILISE'
                WHEN d.DO_Statut = 4 THEN 'LETTRÉ'
                ELSE 'SAISI'
            END AS statut,
            '' AS date_creation,
            '' AS date_modification
        FROM {tbl} d
        WHERE d.DO_Domaine = {ad}
        ORDER BY d.DO_Date DESC
    """.format(tbl=quote_ident(t["documents"]), ad=dc["achat_domain"],
               at=dc["avoir_type"], ttc_expr=ttc_expr, tva_expr=tva_expr)

    try:
        with get_cursor() as cur:
            cur.execute(fallback_query)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()

        invoices = []
        idx = {c: i for i, c in enumerate(columns)}
        _keys = []
        for _r in rows:
            try:
                _keys.append((int(_safe_float(_r[idx["do_type_raw"]])), str(_r[idx["numero"]])))
            except (ValueError, TypeError, KeyError):
                _keys.append((None, None))
        _lines_map = fetch_doc_lines_batch(dc["achat_domain"], [(a, b) for a, b in _keys if a is not None])
        for row in rows:
            inv = dict(zip(columns, [str(v) if v is not None else "" for v in row]))
            inv["tiers_code"] = inv.get("code_tiers", "")
            inv["montant_ht"] = round(_safe_float(row[idx["montant_ht"]]), 2)
            inv["montant_tva"] = round(_safe_float(row[idx["montant_tva"]]), 2)
            inv["montant_ttc"] = round(_safe_float(row[idx["montant_ttc"]]), 2)
            inv["montant_restant"] = round(_safe_float(row[idx["montant_restant"]]), 2)
            _dt = int(_safe_float(row[idx["do_type_raw"]]))
            inv["lignes"] = _lines_map.get((_dt, inv["numero"])) or fetch_doc_lines(dc["achat_domain"], _dt, inv["numero"])
            invoices.append(inv)

        if invoices:
            logger.info("Factures achats fallback chargees: %d", len(invoices))
        else:
            logger.debug("Factures achats fallback chargees: 0")
        return invoices
    except Exception as e:
        logger.error("Erreur requete factures achats (fallback): %s", e)
        return []

_ECRITURES = (
    "write_sfec_to_invoice", "reset_stuck_en_cours", "reset_sfec_monitor",
    "mark_certifying", "mark_to_monitor", "mark_certified",
    "mark_certification_failed",
)

def __getattr__(name):
    if name in _ECRITURES:
        from app.integration.sage import database

        return getattr(database, name)
    raise AttributeError(name)
