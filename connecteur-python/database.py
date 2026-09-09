import re
import threading
import pyodbc
import logging
import time
from datetime import datetime
from contextlib import contextmanager
from config_manager import get_db_config


def _safe_float(val, default=0.0):
    """Convert value to float, handling French comma decimals."""
    if val is None:
        return default
    try:
        return float(str(val).replace(",", "."))
    except (ValueError, TypeError):
        return default

logger = logging.getLogger("t-connector.db")

_pool = None
_pool_lock = threading.Lock()
_db_lock = threading.Lock()


def _sanitize_db_val(val):
    if val is None or isinstance(val, (int, float, bool)):
        return val
    if isinstance(val, str):
        stripped = val.strip()
        if not stripped:
            return val
        has_comma = "," in stripped
        cleaned = stripped.replace(",", ".")
        if has_comma:
            try:
                f = float(cleaned)
                if f == int(f):
                    return int(f)
                return f
            except (ValueError, TypeError):
                pass
        if re.match(r"^-?\d+(\.\d+)?$", cleaned):
            try:
                f = float(cleaned)
                if f == int(f):
                    return int(f)
                return f
            except (ValueError, TypeError):
                pass
    return val


class _SafeRow:
    def __init__(self, row):
        self._row = row

    def __getitem__(self, key):
        return _sanitize_db_val(self._row[key])

    def __len__(self):
        return len(self._row)

    def __iter__(self):
        for i in range(len(self._row)):
            yield _sanitize_db_val(self._row[i])

    def __repr__(self):
        return repr(self._row)


class _SafeCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, *args, **kwargs):
        self._cursor.execute(*args, **kwargs)
        return self

    def executemany(self, *args, **kwargs):
        self._cursor.executemany(*args, **kwargs)
        return self

    def fetchone(self):
        row = self._cursor.fetchone()
        return _SafeRow(row) if row is not None else None

    def fetchall(self):
        return [_SafeRow(row) for row in self._cursor.fetchall()]

    @property
    def description(self):
        return self._cursor.description

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def close(self):
        try:
            self._cursor.close()
        except Exception:
            pass

_TABLE_MAP = None
_TABLE_COLUMNS = {}
_DOMAIN_CONFIG = {
    "vente_domain": 0,
    "achat_domain": 1,
    "facture_type": 6,
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
                        cur.execute("ALTER TABLE [{}] ADD [{}] {}".format(tbl, col_name, col_type))
                        logger.info("Colonne SFEC creee: %s %s", col_name, col_type)
                        changes += 1
                    except Exception as add_err:
                        logger.warning("Impossible d'ajouter %s: %s", col_name, add_err)
                elif not _sfec_type_match(col_type, col_map[key][0], col_map[key][1]):
                    try:
                        cur.execute("ALTER TABLE [{}] ALTER COLUMN [{}] {}".format(tbl, col_name, col_type))
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


def _build_conn_string(cfg):
    import pyodbc as _pyodbc
    available = [d.strip() for d in _pyodbc.drivers()]
    preferred = [
        "ODBC Driver 17 for SQL Server",
        "ODBC Driver 13 for SQL Server",
        "ODBC Driver 11 for SQL Server",
        "SQL Server",
        "SQL Server Native Client 11.0",
    ]
    driver = next((d for d in preferred if d in available), "SQL Server")

    parts = [
        "DRIVER={{{}}}".format(driver),
        "SERVER={}".format(cfg["server"]),
        "DATABASE={}".format(cfg["database"]),
    ]
    if "\\" not in cfg["server"]:
        parts.append("PORT={}".format(cfg.get("port", 1433)))
    if cfg.get("trusted_connection"):
        parts.append("Trusted_Connection=yes")
    else:
        parts.append("UID={}".format(cfg.get("user", "")))
        parts.append("PWD={}".format(cfg.get("password", "")))
    if cfg.get("encrypt"):
        parts.append("Encrypt=yes")
    else:
        parts.append("Encrypt=no")
    if cfg.get("trust_server_certificate"):
        parts.append("TrustServerCertificate=yes")
    return ";".join(parts)


def get_pool():
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.execute("SELECT 1")
                return _pool
            except Exception:
                try:
                    _pool.close()
                except Exception:
                    pass
                _pool = None

        cfg = get_db_config()
        if not cfg.get("server") or not cfg.get("database"):
            raise ValueError("Configuration base de donnees incomplete")

        conn_str = _build_conn_string(cfg)
        timeout = cfg.get("connection_timeout_ms", 15000) // 1000

        _pool = pyodbc.connect(conn_str, timeout=timeout, autocommit=True)
        logger.info("Connexion SQL Server etablie: %s/%s", cfg["server"], cfg["database"])
        return _pool


def close_pool():
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.close()
            except Exception:
                pass
            _pool = None
            logger.info("Connexion SQL Server fermee")


@contextmanager
def get_cursor():
    with _db_lock:
        conn = get_pool()
        cursor = _SafeCursor(conn.cursor())
        try:
            yield cursor
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            cursor.close()


def ping_database():
    try:
        with get_cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        table_map = discover_tables()
        return {"ok": True, "table_map": table_map}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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

        table_map = {"documents": "", "document_lines": "", "third_party": "", "tax_rate": "", "chart_of_accounts": "", "article": ""}

        candidates = {
            "documents": ["dbo_F_DOCENTETE", "F_DOCENTETE", "dbo.F_DOCENTETE"],
            "document_lines": ["dbo_F_DOCLIGNE", "F_DOCLIGNE", "dbo.F_DOCLIGNE"],
            "third_party": ["dbo_F_COMPTET", "F_COMPTET", "dbo.F_COMPTET", "dbo_F_TIERS", "F_TIERS"],
            "tax_rate": ["dbo_F_TVA", "F_TVA", "dbo.F_TVA", "dbo_P_TVA", "P_TVA", "F_TAXE", "dbo_F_TAXE", "dbo.F_TAXE"],
            "chart_of_accounts": ["dbo_F_COMPTEG", "F_COMPTEG", "dbo.F_COMPTEG"],
            "article": ["dbo_F_ARTICLE", "F_ARTICLE", "dbo.F_ARTICLE"],
        }

        like_fallback = {
            "documents": "%DOCENTETE%",
            "document_lines": "%DOCLIGNE%",
            "third_party": "%COMPTET%",
            "tax_rate": "%TAXE%",
            "chart_of_accounts": "%COMPTEG%",
            "article": "%ARTICLE%",
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
                    """.format(table_map["documents"]))
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
    return dict(_DOMAIN_CONFIG)


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

    limit_clause = "TOP {}".format(limit) if limit else ""
    where = "WHERE d.DO_Domaine = {} AND d.DO_Type = {}".format(dc["vente_domain"], dc["facture_type"])

    tiers_col = _get_tiers_col()
    has_third_party = bool(t.get("third_party"))
    has_third_party_cols = bool(_TABLE_COLUMNS.get(t.get("third_party", "").upper(), set()))
    tp_cols = _TABLE_COLUMNS.get(t.get("third_party", "").upper(), set()) if has_third_party else set()

    if tiers_col and has_third_party and has_third_party_cols and "CT_NUM" in tp_cols:
        tiers_select = "d.{} AS code_tiers".format(tiers_col)
        nom_tiers_select = _tiers_name_expr()
        niu_select = "ISNULL(c.CT_NIU, '') AS recipient_niu" if "CT_NIU" in tp_cols else "'' AS recipient_niu"
        phone_select = "ISNULL(c.CT_Telephone, '') AS recipient_phone" if "CT_TELEPHONE" in tp_cols else "'' AS recipient_phone"
        email_select = "ISNULL(c.CT_EMail, '') AS recipient_email" if "CT_EMAIL" in tp_cols else "'' AS recipient_email"
        addr_select = "ISNULL(c.CT_Adresse, '') AS recipient_address" if "CT_ADRESSE" in tp_cols else "'' AS recipient_address"
        classif_select = "ISNULL(c.CT_Statistique05, '') AS recipient_type_raw" if "CT_STATISTIQUE05" in tp_cols else "'' AS recipient_type_raw"
        join_clause = "LEFT JOIN {} c ON d.{} = c.CT_Num".format(t["third_party"], tiers_col)
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
            CAST({vd} AS VARCHAR) + '-' + CAST({ft} AS VARCHAR) + '-' + d.DO_Piece AS id,
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
            {valide},
            {dates}
            {sfec}
        FROM {tbl} d
        {join}
        {where}
        ORDER BY d.DO_Date DESC
    """.format(
        limit=limit_clause, vd=dc["vente_domain"], ft=dc["facture_type"],
        ref=ref_select, tiers=tiers_select, nom_tiers=nom_tiers_select,
        niu=niu_select, phone=phone_select, email=email_select, addr=addr_select,
        classif=classif_select,
        net=net_select, valide=valide_select, dates=date_select,
        sfec=sfec_select, ttc_expr=ttc_expr, tva_expr=tva_expr, tbl=t["documents"], join=join_clause, where=where
    )

    try:
        with get_cursor() as cur:
            cur.execute(query, params)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()

        invoices = []
        for row in rows:
            inv = dict(zip(columns, [str(v) if v is not None else "" for v in row]))
            inv["montant_ht"] = round(_safe_float(row[columns.index("montant_ht")]), 2)
            inv["montant_tva"] = round(_safe_float(row[columns.index("montant_tva")]), 2)
            inv["montant_ttc"] = round(_safe_float(row[columns.index("montant_ttc")]), 2)
            inv["montant_restant"] = round(_safe_float(row[columns.index("montant_restant")]), 2)
            try:
                inv["statut_code"] = int(_safe_float(row[columns.index("statut_code")]))
            except (ValueError, TypeError):
                inv["statut_code"] = 0
            try:
                inv["valide"] = int(_safe_float(row[columns.index("valide")]))
            except (ValueError, TypeError):
                inv["valide"] = 0
            inv["lignes"] = fetch_doc_lines(dc["vente_domain"], dc["facture_type"], inv["numero"])
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

    fallback_query = """
        SELECT TOP 500
            CAST({vd} AS VARCHAR) + '-' + CAST({ft} AS VARCHAR) + '-' + d.DO_Piece AS id,
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
            '' AS date_creation,
            '' AS date_modification,
            '' AS sfec_num_certif,
            '' AS sfec_date_certif,
            '' AS sfec_statut
        FROM {tbl} d
        WHERE d.DO_Domaine = {vd} AND d.DO_Type = {ft}
        ORDER BY d.DO_Piece DESC
    """.format(tbl=t["documents"], vd=dc["vente_domain"], ft=dc["facture_type"], ttc_expr=ttc_expr, tva_expr=tva_expr)

    try:
        with get_cursor() as cur:
            cur.execute(fallback_query)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()

        invoices = []
        for row in rows:
            inv = dict(zip(columns, [str(v) if v is not None else "" for v in row]))
            inv["montant_ht"] = round(_safe_float(row[columns.index("montant_ht")]), 2)
            inv["montant_tva"] = round(_safe_float(row[columns.index("montant_tva")]), 2)
            inv["montant_ttc"] = round(_safe_float(row[columns.index("montant_ttc")]), 2)
            inv["montant_restant"] = round(_safe_float(row[columns.index("montant_restant")]), 2)
            try:
                inv["statut_code"] = int(_safe_float(row[columns.index("statut_code")]))
            except (ValueError, TypeError):
                inv["statut_code"] = 0
            try:
                inv["valide"] = int(_safe_float(row[columns.index("valide")]))
            except (ValueError, TypeError):
                inv["valide"] = 0
            inv["lignes"] = fetch_doc_lines(dc["vente_domain"], dc["facture_type"], inv["numero"])
            invoices.append(inv)

        if invoices:
            logger.info("Factures ventes fallback chargees: %d", len(invoices))
        else:
            logger.debug("Factures ventes fallback chargees: 0")
        return invoices
    except Exception as e:
        logger.error("Erreur requete factures ventes (fallback): %s", e)
        return []


def fetch_doc_lines(domaine, type_doc, piece):
    t = get_table_map()
    if not t["document_lines"]:
        return []

    article_tbl = t.get("article", "")
    if article_tbl:
        join_clause = "LEFT JOIN {} a ON a.AR_Ref = l.AR_Ref".format(article_tbl)
        article_cols = """
            , ISNULL(a.AR_Design, '') AS article_design
            , ISNULL(a.FA_CodeFamille, '') AS article_famille
            , ISNULL(a.AR_Type, 0) AS article_type
            , ISNULL(a.AR_Nature, 0) AS article_nature
        """
    else:
        join_clause = ""
        article_cols = """
            , '' AS article_design
            , '' AS article_famille
            , 0 AS article_type
            , 0 AS article_nature
        """

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
                FROM {tbl} l
                {join}
                WHERE l.DO_Domaine = ?
                  AND l.DO_Type = ?
                  AND l.DO_Piece = ?
                ORDER BY l.DL_Ligne
            """.format(tbl=t["document_lines"], article_cols=article_cols, join=join_clause),
                domaine, type_doc, domaine, type_doc, piece)
            columns = [desc[0] for desc in cur.description]
            result = []
            for row in cur.fetchall():
                line = dict(zip(columns, [str(v) if v is not None else "" for v in row]))
                for key in ("quantite", "prix_unitaire", "montant_ht", "montant_tva", "montant_ttc", "taux_tva"):
                    if key in line:
                        try:
                            line[key] = round(float(str(line[key] or "0").replace(",", ".")), 2)
                        except (ValueError, TypeError):
                            line[key] = 0.0 if key != "taux_tva" else 18.0
                result.append(line)
            return result
    except Exception:
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
        niu_select = "ISNULL(c.CT_NIU, '') AS recipient_niu" if "CT_NIU" in tp_cols else "'' AS recipient_niu"
        phone_select = "ISNULL(c.CT_Telephone, '') AS recipient_phone" if "CT_TELEPHONE" in tp_cols else "'' AS recipient_phone"
        email_select = "ISNULL(c.CT_EMail, '') AS recipient_email" if "CT_EMAIL" in tp_cols else "'' AS recipient_email"
        addr_select = "ISNULL(c.CT_Adresse, '') AS recipient_address" if "CT_ADRESSE" in tp_cols else "'' AS recipient_address"
        classif_select = "ISNULL(c.CT_Statistique05, '') AS recipient_type_raw" if "CT_STATISTIQUE05" in tp_cols else "'' AS recipient_type_raw"
        join_clause = "LEFT JOIN {} c ON d.{} = c.CT_Num".format(t["third_party"], tiers_col)
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
            d.DO_Type AS type_doc,
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
        limit=limit_clause, ad=dc["achat_domain"],
        ref=ref_select, tiers=tiers_select, nom_tiers=nom_tiers_select,
        niu=niu_select, phone=phone_select, email=email_select, addr=addr_select,
        classif=classif_select,
        net=net_select, dates=date_select, ttc_expr=ttc_expr, tva_expr=tva_expr,
        tbl=t["documents"], join=join_clause, where=where
    )

    try:
        with get_cursor() as cur:
            cur.execute(query, params)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()

        invoices = []
        for row in rows:
            inv = dict(zip(columns, [str(v) if v is not None else "" for v in row]))
            inv["montant_ht"] = round(_safe_float(row[columns.index("montant_ht")]), 2)
            inv["montant_tva"] = round(_safe_float(row[columns.index("montant_tva")]), 2)
            inv["montant_ttc"] = round(_safe_float(row[columns.index("montant_ttc")]), 2)
            inv["montant_restant"] = round(_safe_float(row[columns.index("montant_restant")]), 2)
            inv["lignes"] = fetch_doc_lines(dc["achat_domain"], int(_safe_float(row[columns.index("type_doc")])), inv["numero"])
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
            d.DO_Type AS type_doc,
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
    """.format(tbl=t["documents"], ad=dc["achat_domain"], ttc_expr=ttc_expr, tva_expr=tva_expr)

    try:
        with get_cursor() as cur:
            cur.execute(fallback_query)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()

        invoices = []
        for row in rows:
            inv = dict(zip(columns, [str(v) if v is not None else "" for v in row]))
            inv["montant_ht"] = round(_safe_float(row[columns.index("montant_ht")]), 2)
            inv["montant_tva"] = round(_safe_float(row[columns.index("montant_tva")]), 2)
            inv["montant_ttc"] = round(_safe_float(row[columns.index("montant_ttc")]), 2)
            inv["montant_restant"] = round(_safe_float(row[columns.index("montant_restant")]), 2)
            inv["lignes"] = fetch_doc_lines(dc["achat_domain"], int(_safe_float(row[columns.index("type_doc")])), inv["numero"])
            invoices.append(inv)

        if invoices:
            logger.info("Factures achats fallback chargees: %d", len(invoices))
        else:
            logger.debug("Factures achats fallback chargees: 0")
        return invoices
    except Exception as e:
        logger.error("Erreur requete factures achats (fallback): %s", e)
        return []


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
    niu_select = "ISNULL(c.CT_NIU, '') AS numero_fiscal" if "CT_NIU" in tp_cols else "'' AS numero_fiscal"

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
                tbl=t["third_party"], where=where, order=order_by
            ), params)
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, [str(v) if v is not None else "" for v in row])) for row in cur.fetchall()]
    except Exception as e:
        logger.error("Erreur requete contacts: %s", e)
        return []


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
            """.format(p=p, tbl=t["tax_rate"], intitule=intitule_select, taux=taux_select, where=where_inactif))
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
            """.format(tbl=t["chart_of_accounts"], classe=classe_select, collectif=collectif_select))
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, [str(v) if v is not None else "" for v in row])) for row in cur.fetchall()]
    except Exception as e:
        logger.error("Erreur requete plan comptable: %s", e)
        return []


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
        inactif=inactif_s, barcode=barcode_s, tva=tva_s, tbl=article_tbl
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
            for row in cur.fetchall():
                ref = str(row[0]).strip() if row[0] is not None else ""
                for art in articles:
                    if str(art.get("ref", "")).strip() == ref:
                        try:
                            art["stock"] = _safe_float(row[1])
                        except (ValueError, TypeError):
                            art["stock"] = 0.0
                        break
    except Exception as e:
        logger.warning("Stock articles F_ARTSTOCK indisponible: %s", e)


def _parse_invoice_id(invoice_id):
    parts = invoice_id.split("-")
    if len(parts) < 3:
        raise ValueError("ID facture invalide: {}".format(invoice_id))
    return int(parts[0]), int(parts[1]), "-".join(parts[2:])


def write_sfec_to_invoice(invoice_id, certification_number, signature, qr_code, certification_date):
    _refresh_sfec_columns()
    t = get_table_map()
    tbl = t.get("documents", "")
    if not tbl:
        raise ValueError("Table documents introuvable")

    domaine, type_doc, piece = _parse_invoice_id(invoice_id)

    cert_num_str = str(certification_number)[:60] if certification_number else ""
    sig_str = str(signature)[:256] if signature else ""
    qr_str = str(qr_code) if qr_code else ""
    date_str = str(certification_date)[:30] if certification_date else None

    cert_num50 = cert_num_str[:50]
    cert_num69 = cert_num_str[:69]
    sql = """
        UPDATE {} SET
            SFEC_NUM_CERTIF = ?,
            SFEC_NUM_CERTIF1 = ?,
            SFEC_NUM_CERTIF2 = ?,
            SFEC_SIGNATURE = ?,
            SFEC_QR_CODE = ?,
            SFEC_DATE_CERTIF = ?,
            SFEC_DATE_CERTIF1 = ?,
            SFEC_STATUT = 'CERTIFIE'
        WHERE DO_Domaine = ? AND DO_Type = ? AND DO_Piece = ?
    """.format(tbl)
    params = [cert_num50, cert_num50, cert_num69, sig_str, qr_str, date_str, date_str, domaine, type_doc, piece]

    with get_cursor() as cur:
        cur.execute(sql, params)
        if cur.rowcount == 0:
            raise ValueError(
                "WRITE FAILED: 0 ligne pour {} (domaine={}, type={}, piece={})".format(
                    invoice_id, domaine, type_doc, piece))

    logger.info("Ecriture SFEC OK: %s (certif=%s)", invoice_id, cert_num_str[:30])
    return dict(invoice_id=invoice_id, certification_number=cert_num_str,
                signature=sig_str, qr_code=qr_str, certification_date=date_str)


def reset_stuck_en_cours():
    t = get_table_map()
    if not t["documents"]:
        return
    try:
        with get_cursor() as cur:
            cur.execute("""
                UPDATE {} SET SFEC_STATUT = NULL
                WHERE SFEC_STATUT IN ('EN_COURS', 'ERREUR')
                  AND DO_Domaine = 0
                  AND SFEC_DATE_CERTIF IS NULL
                  AND (SFEC_NUM_CERTIF IS NULL OR SFEC_NUM_CERTIF = '')
            """.format(t["documents"]))
            if cur.rowcount > 0:
                logger.info("Statuts bloques remis a NULL: %d factures", cur.rowcount)
    except Exception:
        pass


def mark_certifying(invoice_id):
    t = get_table_map()
    parts = invoice_id.split("-")
    if len(parts) < 3:
        return
    domaine = int(parts[0])
    type_doc = int(parts[1])
    piece = "-".join(parts[2:])
    try:
        with get_cursor() as cur:
            cur.execute("""
                UPDATE {} SET SFEC_STATUT = 'EN_COURS'
                WHERE DO_Domaine = ? AND DO_Type = ? AND DO_Piece = ?
                  AND (SFEC_STATUT IS NULL OR SFEC_STATUT <> 'CERTIFIE')
            """.format(t["documents"]), domaine, type_doc, piece)
    except Exception as e:
        logger.debug("mark_certifying echoue pour %s: %s", invoice_id, e)


def mark_to_monitor(invoice_id):
    t = get_table_map()
    parts = invoice_id.split("-")
    if len(parts) < 3:
        return
    domaine = int(parts[0])
    type_doc = int(parts[1])
    piece = "-".join(parts[2:])
    try:
        with get_cursor() as cur:
            cur.execute("""
                UPDATE {} SET SFEC_STATUT = 'A_SURVEILLER'
                WHERE DO_Domaine = ? AND DO_Type = ? AND DO_Piece = ?
                  AND (SFEC_STATUT IS NULL OR SFEC_STATUT <> 'CERTIFIE')
            """.format(t["documents"]), domaine, type_doc, piece)
    except Exception as e:
        logger.debug("mark_to_monitor echoue pour %s: %s", invoice_id, e)


def mark_certified(invoice_id, note="CERTIFIE"):
    t = get_table_map()
    parts = invoice_id.split("-")
    if len(parts) < 3:
        return
    domaine = int(parts[0])
    type_doc = int(parts[1])
    piece = "-".join(parts[2:])
    try:
        with get_cursor() as cur:
            cur.execute("""
                UPDATE {} SET SFEC_STATUT = ?
                WHERE DO_Domaine = ? AND DO_Type = ? AND DO_Piece = ?
                  AND (SFEC_STATUT IS NULL OR SFEC_STATUT NOT IN ('CERTIFIE', ?))
            """.format(t["documents"]), note, domaine, type_doc, piece, note)
        logger.info("Facture marquee %s: %s", note, invoice_id)
    except Exception as e:
        logger.debug("mark_certified echoue pour %s: %s", invoice_id, e)


def mark_certification_failed(invoice_id, error_msg):
    t = get_table_map()
    parts = invoice_id.split("-")
    if len(parts) < 3:
        return
    domaine = int(parts[0])
    type_doc = int(parts[1])
    piece = "-".join(parts[2:])
    try:
        with get_cursor() as cur:
            cur.execute("""
                UPDATE {} SET SFEC_STATUT = 'ERREUR'
                WHERE DO_Domaine = ? AND DO_Type = ? AND DO_Piece = ?
                  AND (SFEC_STATUT IS NULL OR SFEC_STATUT NOT IN ('CERTIFIE', 'DEJA_CERTIFIE'))
            """.format(t["documents"]), domaine, type_doc, piece)
    except Exception as e:
        logger.debug("mark_certification_failed echoue pour %s: %s", invoice_id, e)
    logger.warning("Facture marquee ERREUR certification SFEC: %s - %s", invoice_id, error_msg)


def fetch_certified_invoices():
    _refresh_sfec_columns()
    t = get_table_map()
    dc = get_domain_config()

    has_sfec = _detect_sfec_columns(t.get("documents", ""))
    if not has_sfec:
        return []

    try:
        with get_cursor() as cur:
            cur.execute("""
                SELECT
                    CAST(DO_Domaine AS VARCHAR) + '-' + CAST(DO_Type AS VARCHAR) + '-' + DO_Piece AS id,
                    SFEC_NUM_CERTIF AS certif_num,
                    SFEC_SIGNATURE AS signature,
                    SFEC_QR_CODE AS qr_code,
                    ISNULL(TRY_CONVERT(VARCHAR(30), SFEC_DATE_CERTIF, 126), '') AS date
                FROM {}
                WHERE DO_Domaine = ?
                  AND DO_Type = ?
                  AND SFEC_STATUT IN ('CERTIFIE', 'DEJA_CERTIFIE')
            """.format(t["documents"]), dc["vente_domain"], dc["facture_type"])
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, [str(v) if v is not None else "" for v in row])) for row in cur.fetchall()]
    except Exception:
        return []


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
