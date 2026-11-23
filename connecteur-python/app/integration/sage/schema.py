"""Schéma et découverte des tables Sage 100 (_TABLE_MAP, colonnes SFEC)."""
import logging
import re

from app.config.manager import get_db_config
from app.integration.sage.connection import get_cursor
from app.integration.sage.errors import SageSchemaError
from app.integration.sage.sql import quote_ident

logger = logging.getLogger("t-connector.db")


_TABLE_MAP = None

_TABLE_COLUMNS = {}

_DOMAIN_CONFIG = {
    "vente_domain": 0,
    "achat_domain": 1,
    "facture_type": 6,
    "avoir_type": 7,
    "retour_type": 8,
}

_SFEC_COLUMNS = [
    ("SFEC_NUM_CERTIF", "VARCHAR(50) NULL"),
    ("SFEC_NUM_CERTIF1", "VARCHAR(50) NULL"),
    ("SFEC_NUM_CERTIF2", "VARCHAR(69) NULL"),
    ("SFEC_SIGNATURE", "VARCHAR(256) NULL"),
    ("SFEC_QR_CODE", "VARCHAR(MAX) NULL"),
    ("SFEC_DATE_CERTIF", "DATETIME NULL"),
    ("SFEC_DATE_CERTIF1", "DATETIME NULL"),
    ("SFEC_STATUT", "VARCHAR(20) NULL"),
]

def _sfec_type_match(expected, actual_type, actual_len):
    exp_base = expected.split()[0].upper()
    if exp_base == "DATETIME":
        return actual_type.upper() == "DATETIME"
    if "VARCHAR" in exp_base:
        if actual_type.upper() not in ("VARCHAR", "NVARCHAR"):
            return False
        if "MAX" in exp_base:
            return actual_len == 0 or actual_len == -1
        exp_len = int(exp_base.replace("VARCHAR(", "").replace(")", ""))
        if actual_len is None and exp_len > 8000:
            return True
        if actual_len is None:
            return False
        return actual_len >= exp_len
    return expected.strip().upper().split()[0] == actual_type.upper()

def ensure_sfec_columns(tbl=None):
    if tbl is None:
        t = get_table_map()
        tbl = t.get("documents", "")
    if not tbl:
        return False
    tbl_upper = tbl.upper()
    changes = 0
    try:
        with get_cursor() as cur:
            cur.execute("""
                SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH
                FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ?
            """, tbl)
            col_map = {row[0].upper(): (row[1], row[2]) for row in cur.fetchall()}
            for col_name, col_type in _SFEC_COLUMNS:
                key = col_name.upper()
                if key not in col_map:
                    try:
                        cur.execute("ALTER TABLE {} ADD {} {}".format(
                            quote_ident(tbl), quote_ident(col_name), col_type))
                        logger.info("Colonne SFEC creee: %s %s", col_name, col_type)
                        changes += 1
                    except Exception as add_err:
                        logger.warning("Impossible d'ajouter %s: %s", col_name, add_err)
                elif not _sfec_type_match(col_type, col_map[key][0], col_map[key][1]):
                    try:
                        cur.execute("ALTER TABLE {} ALTER COLUMN {} {}".format(
                            quote_ident(tbl), quote_ident(col_name), col_type))
                        logger.info("Colonne SFEC alteree: %s %s", col_name, col_type)
                        changes += 1
                    except Exception as alter_err:
                        logger.warning("ALTER COLUMN %s ignore: %s", col_name, alter_err)
            cols = _TABLE_COLUMNS.get(tbl_upper)
            if cols is not None:
                for col_name, _ in _SFEC_COLUMNS:
                    cols.add(col_name.upper())
    except Exception as e:
        logger.error("Auto-migration SFEC echouee: %s", e)
        return False
    if changes:
        logger.info("Auto-migration SFEC: %d modifications", changes)
    return True

def _discover_columns(table_name):
    """Retourne un set des noms de colonnes d'une table (upper case)."""
    if not table_name:
        return set()
    try:
        with get_cursor() as cur:
            cur.execute("""
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = ?
            """, table_name)
            return {row[0].upper() for row in cur.fetchall()}
    except Exception:
        return set()

def _has_col(table_key, col_name):
    """Verifie si une colonne existe dans une table decouverte."""
    t = get_table_map()
    real_table = t.get(table_key, "")
    if not real_table:
        return False
    cols = _TABLE_COLUMNS.get(real_table.upper(), set())
    return col_name.upper() in cols

def _col_expr(table_alias, col_name, fallback_default="''", alias=None):
    """Genere une expression SQL pour une colonne, avec fallback si absente."""
    if alias is None:
        alias = col_name.lower()
    cols = _TABLE_COLUMNS.get(table_alias.upper(), set()) if isinstance(table_alias, str) else set()
    if col_name.upper() in cols:
        return "{}.{}".format(table_alias, col_name)
    return "ISNULL({},{})".format(fallback_default, alias)

def _detect_sfec_columns(table_name):
    if not table_name:
        return False
    cols = _TABLE_COLUMNS.get(table_name.upper(), set())
    return "SFEC_STATUT" in cols

def _status_label(statut_code):
    mapping = {0: "SAISI", 1: "CONFIRME", 2: "A COMPTABILISER", 3: "COMPTABILISE", 4: "LETTRÉ"}
    return mapping.get(statut_code, "SAISI")

def discover_tables():
    global _TABLE_MAP, _TABLE_COLUMNS
    try:
        with get_cursor() as cur:
            cur.execute("""
                SELECT TABLE_NAME
                FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_TYPE = 'BASE TABLE'
            """)
            existing = {row[0].upper() for row in cur.fetchall()}

        table_map = {"documents": "", "document_lines": "", "third_party": "", "tax_rate": "", "chart_of_accounts": "", "article": "", "colonne": ""}

        candidates = {
            "documents": ["dbo_F_DOCENTETE", "F_DOCENTETE", "dbo.F_DOCENTETE"],
            "document_lines": ["dbo_F_DOCLIGNE", "F_DOCLIGNE", "dbo.F_DOCLIGNE"],
            "third_party": ["dbo_F_COMPTET", "F_COMPTET", "dbo.F_COMPTET", "dbo_F_TIERS", "F_TIERS"],
            "tax_rate": ["dbo_F_TVA", "F_TVA", "dbo.F_TVA", "dbo_P_TVA", "P_TVA", "F_TAXE", "dbo_F_TAXE", "dbo.F_TAXE"],
            "chart_of_accounts": ["dbo_F_COMPTEG", "F_COMPTEG", "dbo.F_COMPTEG"],
            "article": ["dbo_F_ARTICLE", "F_ARTICLE", "dbo.F_ARTICLE"],
            "colonne": ["dbo_F_COLONNE", "F_COLONNE", "dbo.F_COLONNE"],
        }

        like_fallback = {
            "documents": "%DOCENTETE%",
            "document_lines": "%DOCLIGNE%",
            "third_party": "%COMPTET%",
            "tax_rate": "%TAXE%",
            "chart_of_accounts": "%COMPTEG%",
            "article": "%ARTICLE%",
            "colonne": "%COLONNE%",
        }

        for logical, names in candidates.items():
            found = False
            for name in names:
                if name.upper() in existing:
                    table_map[logical] = name
                    found = True
                    break
            if not found and logical in like_fallback:
                pattern = like_fallback[logical]
                with get_cursor() as cur:
                    cur.execute("""
                        SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
                        WHERE TABLE_TYPE = 'BASE TABLE' AND TABLE_NAME LIKE ?
                        ORDER BY TABLE_NAME
                    """, pattern)
                    candidates_like = [row[0] for row in cur.fetchall()]

                if logical == "third_party":
                    candidates_like = [
                        n for n in candidates_like
                        if not any(s in n.upper() for s in
                                   ("INFOS", "ARCHIVE", "HISTO", "TICKET"))
                    ]

                for name in candidates_like:
                    cols = _discover_columns(name)
                    if logical == "third_party" and not (cols & {"CT_TYPE", "CT_INTITULE", "CT_NOM"}):
                        continue
                    if logical == "colonne" and "CO_LIBELLE" not in cols:
                        continue
                    table_map[logical] = name
                    found = True
                    logger.info("Table trouvee par LIKE '%s': %s", pattern, name)
                    break

        _TABLE_MAP = table_map
        logger.info("Tables Sage 100: %s", table_map)

        for logical, real_name in table_map.items():
            if real_name:
                cols = _discover_columns(real_name)
                _TABLE_COLUMNS[real_name.upper()] = cols
                logger.info("Colonnes %s: %d colonnes", real_name, len(cols))

        missing = []
        doc_cols = _TABLE_COLUMNS.get(table_map.get("documents", "").upper(), set())
        if table_map["documents"]:
            has_do_tiers = "DO_TIERS" in doc_cols
            has_ct_num = "CT_NUM" in doc_cols
            if not has_do_tiers and not has_ct_num:
                missing.append("ni DO_Tiers ni CT_Num dans {}".format(table_map["documents"]))
            elif has_ct_num and not has_do_tiers:
                logger.info("Colonne tiers: CT_Num dans %s", table_map["documents"])
            else:
                logger.info("Colonne tiers: DO_Tiers dans %s", table_map["documents"])

            has_modif = "CBMODIFICATION" in doc_cols
            if not has_modif:
                missing.append("cbModification absent de {}".format(table_map["documents"]))

            has_valide = "DO_VALIDE" in doc_cols
            if not has_valide:
                missing.append("DO_Valide absent de {}".format(table_map["documents"]))

        if missing:
            logger.warning("Colonnes manquantes: %s", "; ".join(missing))
            logger.info("Requetes adaptees avec fallbacks automatiques")

        if table_map["documents"]:
            try:
                with get_cursor() as cur:
                    cur.execute("""
                        SELECT DO_Domaine, DO_Type, COUNT(*) AS cnt
                        FROM {}
                        GROUP BY DO_Domaine, DO_Type
                        ORDER BY DO_Domaine, DO_Type
                    """.format(quote_ident(table_map["documents"])))
                    for row in cur.fetchall():
                        domain_label = "Vente" if row[0] == 0 else "Achat" if row[0] == 1 else "Domaine {}".format(row[0])
                        logger.info("  %s Type %s: %s documents", domain_label, row[1], row[2])
            except Exception as e:
                logger.warning("Impossible de lister les types de documents: %s", e)

        if table_map["documents"]:
            try:
                ensure_sfec_columns(table_map["documents"])
            except Exception as e:
                logger.warning("ensure_sfec_columns: %s", e)

        return table_map
    except Exception as e:
        logger.error("Erreur decouverte tables: %s", e)
        return {"documents": "dbo_F_DOCENTETE", "document_lines": "dbo_F_DOCLIGNE", "third_party": "dbo_F_COMPTET", "tax_rate": "dbo_F_TVA", "chart_of_accounts": "dbo_F_COMPTEG"}

def get_table_map():
    global _TABLE_MAP
    if _TABLE_MAP is None:
        _TABLE_MAP = discover_tables()
    return _TABLE_MAP

def get_domain_config():
    """Types de documents Sage, surchargeables par `config.json`.

    Modele client (table Sage des types de pieces) :
      - 6 = facture de vente
      - 7 = facture d'avoir de vente
      - 8 = facture de retour de vente (impact stock gere par Sage)

    La surcharge `db.sage_facture_type`, `db.sage_avoir_type` et
    `db.sage_retour_type` permet d'adapter le connecteur a un autre
    parametrage Sage sans toucher au code.
    """
    config = dict(_DOMAIN_CONFIG)
    try:
        from app.config.manager import get_config
        db_config = (get_config().get("db") or {})
    except Exception:
        db_config = {}
    for key, config_key in (
        ("facture_type", "sage_facture_type"),
        ("avoir_type", "sage_avoir_type"),
        ("retour_type", "sage_retour_type"),
    ):
        value = db_config.get(config_key)
        if value in (None, ""):
            continue
        try:
            config[key] = int(value)
        except (TypeError, ValueError):
            continue
    return config

def _doc_types(config, keys):
    """Liste de types de pieces (sans doublon) pour une liste de cles."""
    types = []
    for key in keys:
        value = config.get(key)
        if value in (None, ""):
            continue
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value not in types:
            types.append(value)
    return types

def sales_doc_types(config=None):
    """Types de pieces de vente lus par le connecteur (facture, avoir, retour)."""
    return _doc_types(config or get_domain_config(),
                      ("facture_type", "avoir_type", "retour_type"))

def credit_doc_types(config=None):
    """Types de pieces qui sont des remboursements (creditNote SFEC)."""
    return _doc_types(config or get_domain_config(),
                      ("avoir_type", "retour_type"))

def _get_tiers_col():
    """Detecte la colonne tiers dans F_DOCENTETE: CT_Num ou DO_Tiers."""
    t = get_table_map()
    doc_cols = _TABLE_COLUMNS.get(t.get("documents", "").upper(), set())
    if "CT_NUM" in doc_cols:
        return "CT_Num"
    if "DO_TIERS" in doc_cols:
        return "DO_Tiers"
    return None

def _tiers_name_expr(alias="c"):
    """Genere l'expression SQL du nom du tiers selon les colonnes disponibles."""
    t = get_table_map()
    tp_table = t.get("third_party", "")
    tp_cols = _TABLE_COLUMNS.get(tp_table.upper(), set()) if tp_table else set()
    for col in ("CT_Intitule", "CT_IntitulePayeur", "CT_RaisonSociale", "CT_Libelle"):
        if col.upper() in tp_cols:
            return "ISNULL({}.{}, '') AS nom_tiers".format(alias, col)
    return "'' AS nom_tiers"

def _refresh_sfec_columns():
    try:
        ensure_sfec_columns()
    except Exception:
        pass

def list_all_tables():
    try:
        with get_cursor() as cur:
            cur.execute("""
                SELECT TABLE_NAME
                FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_TYPE = 'BASE TABLE'
                ORDER BY TABLE_NAME
            """)
            return [row[0] for row in cur.fetchall()]
    except Exception:
        return []
