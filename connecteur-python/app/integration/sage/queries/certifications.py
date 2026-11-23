"""Factures certifiées SFEC déjà présentes dans Sage 100."""
import logging

from app.integration.sage.connection import get_cursor
from app.integration.sage.schema import (
    _detect_sfec_columns, _refresh_sfec_columns, credit_doc_types,
    get_domain_config, get_table_map, sales_doc_types,
)
from app.integration.sage.sql import quote_ident

logger = logging.getLogger("t-connector.db")


def fetch_certified_invoices():
    _refresh_sfec_columns()
    t = get_table_map()
    dc = get_domain_config()

    has_sfec = _detect_sfec_columns(t.get("documents", ""))
    if not has_sfec:
        return []

    try:
        doc_types = sales_doc_types(dc)
        credit_types = credit_doc_types(dc)
        if not doc_types:
            return []
        types_ph = ", ".join("?" for _ in doc_types)
        credit_ph = ", ".join("?" for _ in credit_types) if credit_types else types_ph
        params = list(credit_types) + [dc["vente_domain"]] + list(doc_types)
        with get_cursor() as cur:
            cur.execute("""
                SELECT
                    CAST(DO_Domaine AS VARCHAR) + '-' + CAST(DO_Type AS VARCHAR) + '-' + DO_Piece AS id,
                    SFEC_NUM_CERTIF AS certif_num,
                    SFEC_SIGNATURE AS signature,
                    SFEC_QR_CODE AS qr_code,
                    CASE WHEN DO_Type IN ({credit_ph}) THEN 'creditNote' ELSE 'salesInvoice' END AS invoice_type,
                    ISNULL(TRY_CONVERT(VARCHAR(30), SFEC_DATE_CERTIF, 126), '') AS date
                FROM {}
                WHERE DO_Domaine = ?
                  AND DO_Type IN ({types_ph})
                  AND SFEC_STATUT IN ('CERTIFIE', 'DEJA_CERTIFIE')
            """.format(quote_ident(t["documents"]), credit_ph=credit_ph, types_ph=types_ph),
                       params)
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, [str(v) if v is not None else "" for v in row])) for row in cur.fetchall()]
    except Exception:
        return []


# Les écritures et marquages SFEC restent dans la façade `database` (ils
# partagent le verrou `_db_lock`). Import tardif pour éviter le cycle
# `database -> queries.invoices`.
