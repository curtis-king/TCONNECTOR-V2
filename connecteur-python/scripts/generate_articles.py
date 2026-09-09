"""Génère des articles dans la base SQL Server Sage 100.

Usage (depuis connecteur-python):
    python scripts/generate_articles.py --count 20 [--db DIMI|SER] [--family FAM] [--dry-run]

Par défaut, utilise la base configurée dans config.json (section db.database).
L'option --db permet de cibler une autre base (ex: DIMI) tout en gardant le reste
de la configuration (serveur, authentification).
"""

import os
import sys

# Racine du projet = un niveau au-dessus de ce script (scripts/).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import argparse
import logging
import re
import traceback

import pyodbc

from config_manager import get_db_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("t-connector.generate")


def _build_conn(cfg, target_db=None):
    available = [d.strip() for d in pyodbc.drivers()]
    preferred = [
        "ODBC Driver 17 for SQL Server",
        "ODBC Driver 13 for SQL Server",
        "ODBC Driver 11 for SQL Server",
        "SQL Server Native Client 11.0",
        "SQL Server",
    ]
    driver = next((d for d in preferred if d in available), "SQL Server")
    database = target_db or cfg.get("database", "")
    parts = [
        "DRIVER={{{}}}".format(driver),
        "SERVER={}".format(cfg["server"]),
        "DATABASE={}".format(database),
    ]
    if "\\" not in cfg["server"]:
        parts.append("PORT={}".format(cfg.get("port", 1433)))
    if cfg.get("trusted_connection"):
        parts.append("Trusted_Connection=yes")
    else:
        parts.append("UID={}".format(cfg.get("user", "")))
        parts.append("PWD={}".format(cfg.get("password", "")))
    parts.append("Encrypt=yes" if cfg.get("encrypt") else "Encrypt=no")
    if cfg.get("trust_server_certificate"):
        parts.append("TrustServerCertificate=yes")
    timeout = cfg.get("connection_timeout_ms", 15000) // 1000
    conn_str = ";".join(parts)
    return pyodbc.connect(conn_str, timeout=timeout, autocommit=True)


def _ensure_family(cur, family_code, family_label):
    cur.execute("SELECT COUNT(*) FROM F_FAMILLE WHERE FA_CodeFamille = ?", (family_code,))
    if cur.fetchone()[0] > 0:
        return False
    try:
        cur.execute("""
            INSERT INTO F_FAMILLE (FA_CodeFamille, FA_Type, FA_Intitule, FA_UniteVen, FA_Coef, FA_Publie)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (family_code, 1, family_label, 0, 1, 1))
        return True
    except Exception as e:
        logger.warning("Insert famille via triggers echoue (%s), nouvel essai sans triggers", e)
        cur.execute("ALTER TABLE F_FAMILLE DISABLE TRIGGER ALL")
        try:
            cur.execute("""
                INSERT INTO F_FAMILLE (FA_CodeFamille, FA_Type, FA_Intitule, FA_UniteVen, FA_Coef, FA_Publie)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (family_code, 1, family_label, 0, 1, 1))
            return True
        finally:
            cur.execute("ALTER TABLE F_FAMILLE ENABLE TRIGGER ALL")


def generate_articles(count=20, target_db=None, family_code="1", family_label="FAMILLE DEFAUT",
                      prefix="ART", dry_run=False):
    cfg = get_db_config()
    if not cfg.get("server") or not cfg.get("database"):
        raise ValueError("Configuration base de donnees incomplete (config.json)")

    conn = _build_conn(cfg, target_db)
    try:
        cur = conn.cursor()

        # Verifier que la table F_ARTICLE existe
        cur.execute("""
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_NAME = 'F_ARTICLE' AND TABLE_TYPE = 'BASE TABLE'
        """)
        if cur.fetchone()[0] == 0:
            raise RuntimeError("Table F_ARTICLE introuvable dans la base cible")

        # Famille
        if not dry_run:
            created_fam = _ensure_family(cur, family_code, family_label)
            if created_fam:
                logger.info("Famille creee : %s (%s)", family_code, family_label)
        else:
            logger.info("[dry-run] Famille %s verifiee (pas d'insert)", family_code)

        # Referentiel de prix/désignations pour des articles réalistes
        catalog = [
            ("Riz parfumé 5kg", 4250, 5000),
            ("Huile végétale 1L", 1350, 1600),
            ("Sucre en poudre 1kg", 750, 950),
            ("Lait concentré sucré", 850, 1050),
            ("Savon de toilette", 350, 450),
            ("Eau minérale 1.5L", 500, 650),
            ("Tomate concentrée", 650, 800),
            ("Farine de blé 1kg", 900, 1100),
            ("Pâtes alimentaires 500g", 550, 700),
            ("Thé vert sachet", 1500, 1900),
            ("Café soluble 100g", 1800, 2200),
            ("Mayonnaise 250g", 1900, 2300),
            ("Biscuits sucrés", 700, 900),
            ("Pomme de terre 1kg", 1200, 1500),
            ("Oignon blanc 1kg", 650, 850),
            ("Poisson fumé 1kg", 3500, 4200),
            ("Poulet congelé 1kg", 2800, 3400),
            ("Bœuf 1kg", 4500, 5300),
            ("Sel de cuisine 1kg", 300, 400),
            ("Oeufs boîte de 10", 1200, 1500),
            ("Jus d'orange 1L", 1500, 1850),
            ("Bonbon assortiment", 250, 350),
            ("Arachide grillée 500g", 1500, 1900),
            ("Maïs 1kg", 700, 900),
            ("Haricot rouge 1kg", 1100, 1350),
            ("Chocolat en poudre", 2200, 2600),
            ("Pain de mie 400g", 1100, 1400),
            ("Margarine 250g", 950, 1200),
            ("Concentré de tomate 2x", 850, 1050),
            ("Amidon 1kg", 900, 1100),
        ]

        # Récupérer les références existantes pour éviter les doublons
        cur.execute("SELECT AR_Ref FROM F_ARTICLE")
        existing = {str(r[0]).strip() for r in cur.fetchall()}

        # Déterminer le prochain index disponible (basé sur le préfixe)
        idx = 1
        if existing:
            pat = re.compile(r"^" + re.escape(prefix) + r"-(\d+)$")
            for ref in existing:
                m = pat.match(ref)
                if m:
                    idx = max(idx, int(m.group(1)) + 1)

        rows = []
        for i in range(count):
            ref = "{}-{:04d}".format(prefix, idx)
            idx += 1
            if ref in existing:
                continue
            design, pa, pv = catalog[(idx - 1) % len(catalog)]
            rows.append((
                ref, design, family_code, float(pa), float(pv),
                1,  # AR_Type
                1,  # AR_Nature
                1,  # AR_SuiviStock
                1,  # AR_Publie
            ))

        if not rows:
            logger.info("Aucun article a inserer (tous les references existent deja)")
            return {"inserted": 0, "existing": len(existing), "family": family_code}

        if dry_run:
            logger.info("[dry-run] %d articles prets a etre inseres (rien n'est ecrit)", len(rows))
            for ref, design, fam, pa, pv, *_ in rows:
                logger.info("  - %s | %s | PA=%s PV=%s", ref, design, pa, pv)
            return {"dry_run": True, "would_insert": len(rows), "family": family_code}

        # Insertion : désactiver temporairement les triggers Sage si l'insert direct échoue
        inserted = 0
        try:
            cur.execute("""
                INSERT INTO F_ARTICLE
                    (AR_Ref, AR_Design, FA_CodeFamille, AR_PrixAch, AR_PrixVen,
                     AR_Type, AR_Nature, AR_SuiviStock, AR_Publie)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows[0])
            inserted += 1
            for row in rows[1:]:
                cur.execute("""
                    INSERT INTO F_ARTICLE
                        (AR_Ref, AR_Design, FA_CodeFamille, AR_PrixAch, AR_PrixVen,
                         AR_Type, AR_Nature, AR_SuiviStock, AR_Publie)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, row)
                inserted += 1
        except Exception as e:
            logger.warning("Insert direct echoue (%s), bascule en mode triggers desactives", e)
            cur.execute("ALTER TABLE F_ARTICLE DISABLE TRIGGER ALL")
            try:
                inserted = 0
                for row in rows:
                    cur.execute("""
                        INSERT INTO F_ARTICLE
                            (AR_Ref, AR_Design, FA_CodeFamille, AR_PrixAch, AR_PrixVen,
                             AR_Type, AR_Nature, AR_SuiviStock, AR_Publie)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, row)
                    inserted += 1
            finally:
                cur.execute("ALTER TABLE F_ARTICLE ENABLE TRIGGER ALL")

        logger.info("Insertion terminee : %d articles", inserted)
        return {"inserted": inserted, "existing_before": len(existing),
                "family": family_code, "target_db": target_db or cfg.get("database")}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Generer des articles dans SQL Server Sage")
    parser.add_argument("--count", type=int, default=20, help="Nombre d'articles (defaut 20)")
    parser.add_argument("--db", default=None, help="Base cible (par defaut : config.json db.database)")
    parser.add_argument("--family", default="1", help="Code famille (defaut '1')")
    parser.add_argument("--family-label", default="FAMILLE DEFAUT", help="Libelle famille")
    parser.add_argument("--prefix", default="ART", help="Prefixe des references (defaut ART)")
    parser.add_argument("--dry-run", action="store_true", help="Affiche les articles sans les inserer")
    args = parser.parse_args()

    try:
        result = generate_articles(
            count=args.count,
            target_db=args.db,
            family_code=args.family,
            family_label=args.family_label,
            prefix=args.prefix,
            dry_run=args.dry_run,
        )
        logger.info("Resultat : %s", result)
    except Exception as e:
        logger.error("Echec : %s", e)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
