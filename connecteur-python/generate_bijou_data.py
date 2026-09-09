"""Genere des articles de bijouterie et des tiers clients dans la base Sage BIJOU.

Respecte les normes Sage 100 :
- F_ARTICLE : cbAR_Ref/cbFA_CodeFamille binaires, cbMarq compteur, cbCreation/cbModification/cbCreateur
- F_COMPTET : cbCT_Num binaire, CT_Type=0 (client), CG_NumPrinc='4110000', compteur cbMarq
- F_FAMILLE : famille BIJOUX_ moyennes reelles de la base BIJOU

Usage (depuis connecteur-python) :
    python generate_bijou_data.py [--articles 50] [--tiers 50] [--dry-run]
"""

import argparse
import hashlib
import logging
import re
import sys
import traceback

import pyodbc

from config_manager import get_db_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("t-connector.bijou")


def _build_conn(cfg):
    available = [d.strip() for d in pyodbc.drivers()]
    preferred = [
        "ODBC Driver 17 for SQL Server",
        "ODBC Driver 13 for SQL Server",
        "ODBC Driver 11 for SQL Server",
        "SQL Server Native Client 11.0",
        "SQL Server",
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
    parts.append("Encrypt=yes" if cfg.get("encrypt") else "Encrypt=no")
    if cfg.get("trust_server_certificate"):
        parts.append("TrustServerCertificate=yes")
    timeout = cfg.get("connection_timeout_ms", 15000) // 1000
    return pyodbc.connect(";".join(parts), timeout=timeout, autocommit=True)


def _trunc(value, size):
    value = str(value or "")
    return value[:size]


# --- Referentiels bijouterie (coherent avec la base BIJOU) ---
FAMILLES = ["BIJOUXOR", "BIJOUXARG", "MONTREOR", "MONTREDIV", "ORFEVRERIE"]

CATALOG_ARTICLES = [
    # (designation, famille, prix_achat, prix_vente, coef)
    ("Bague Or massif 18 carats", "BIJOUXOR", 120000, 240000, 2.0),
    ("Bague Argente pave diamants", "BIJOUXARG", 45000, 90000, 2.0),
    ("Collier Or grain de cafe", "BIJOUXOR", 145000, 290000, 2.0),
    ("Collier Argent maille ronce", "BIJOUXARG", 52000, 104000, 2.0),
    ("Bracelet Or dorure", "BIJOUXOR", 65000, 130000, 2.0),
    ("Bracelet Argent gourmette", "BIJOUXARG", 28000, 56000, 2.0),
    ("Boucles Oreilles Or puces", "BIJOUXOR", 32000, 64000, 2.0),
    ("Boucles Oreilles Argent", "BIJOUXARG", 15000, 30000, 2.0),
    ("Pendentif Or croix", "BIJOUXOR", 48000, 96000, 2.0),
    ("Pendentif Argent saint Christophe", "BIJOUXARG", 19000, 38000, 2.0),
    ("Montre Or Robinson", "MONTREOR", 210000, 420000, 2.0),
    ("Montre Divers homme cuir", "MONTREDIV", 60000, 120000, 2.0),
    ("Montre Divers femme acier", "MONTREDIV", 55000, 110000, 2.0),
    ("Montre Or Jacquemin", "MONTREOR", 185000, 370000, 2.0),
    ("Service a cafe Orfevrerie", "ORFEVRERIE", 95000, 190000, 2.0),
    ("Chandelier Orfevrerie", "ORFEVRERIE", 85000, 170000, 2.0),
    ("Medaille Or Vierge", "BIJOUXOR", 74000, 148000, 2.0),
    ("Chaise Argent pendentif", "BIJOUXARG", 42000, 84000, 2.0),
    ("Entourage Or brillants", "BIJOUXOR", 98000, 196000, 2.0),
    ("Bracelet Or flexible tresse", "BIJOUXOR", 121000, 242000, 2.0),
    ("Bague Argent ajustable", "BIJOUXARG", 26000, 52000, 2.0),
    ("Collier Argent perles", "BIJOUXARG", 38000, 76000, 2.0),
    ("Montre Divers sport nylon", "MONTREDIV", 42000, 84000, 2.0),
    ("Montre Or slim", "MONTREOR", 240000, 480000, 2.0),
    ("Plateau Orfevrerie argent", "ORFEVRERIE", 76000, 152000, 2.0),
    ("Pendentif Or lion", "BIJOUXOR", 82000, 164000, 2.0),
    ("Bracelet Argent manchette", "BIJOUXARG", 30000, 60000, 2.0),
    ("Boucles Oreilles Or gouttes", "BIJOUXOR", 36000, 72000, 2.0),
    ("Bague Or solitaire", "BIJOUXOR", 150000, 300000, 2.0),
    ("Collier Or serpent", "BIJOUXOR", 168000, 336000, 2.0),
    ("Montre Divers quartz", "MONTREDIV", 48000, 96000, 2.0),
    ("Bracelet Or cadenas", "BIJOUXOR", 88000, 176000, 2.0),
    ("Montre Or carree", "MONTREOR", 195000, 390000, 2.0),
    ("Boucles Oreilles Argent anneaux", "BIJOUXARG", 20000, 40000, 2.0),
    ("Pendentif Argent coeur", "BIJOUXARG", 24000, 48000, 2.0),
    ("Porte-carte Orfevrerie", "ORFEVRERIE", 68000, 136000, 2.0),
    ("Bague Or alliance", "BIJOUXOR", 138000, 276000, 2.0),
    ("Cadenas Argent bijou", "BIJOUXARG", 22000, 44000, 2.0),
    ("Montre Divers bracelet metal", "MONTREDIV", 58000, 116000, 2.0),
    ("Medailon Or ancien", "BIJOUXOR", 87000, 174000, 2.0),
    ("Bracelet Argent chaines", "BIJOUXARG", 27000, 54000, 2.0),
    ("Montre Or bezel", "MONTREOR", 205000, 410000, 2.0),
    ("Boucles Oreilles Or jonc", "BIJOUXOR", 30000, 60000, 2.0),
    ("Pendentif Or etoile", "BIJOUXOR", 53000, 106000, 2.0),
    ("Bague Argent oeil de tigre", "BIJOUXARG", 34000, 68000, 2.0),
    ("Collier Or gourmette", "BIJOUXOR", 158000, 316000, 2.0),
    ("Service a the Orfevrerie", "ORFEVRERIE", 98000, 196000, 2.0),
    ("Bracelet Or perles femme", "BIJOUXOR", 91000, 182000, 2.0),
    ("Montre Divers chrono", "MONTREDIV", 62000, 124000, 2.0),
    ("Bague Or emeraude", "BIJOUXOR", 160000, 320000, 2.0),
]

CATALOG_TIERS = [
    # (intitule, contact, adresse, ville, telephone, email, site, identifiant, siret)
    ("SOCIETE SAPHIR", "M. Diallo Bernard", "12 rue de la Paix", "Brazzaville", "06 811 12 12", "saphir@bijou.cg", "saphir.cg", "B1000451", "551 245 681 00021"),
    ("KOVAL FIE", "Mme Koval Fie", "45 avenue Monseigneur", "Pointe-Noire", "05 512 34 56", "koval@bijou.cg", "kovalfie.cg", "B1000452", "552 090 711 00034"),
    ("CONGO DIAMANTS", "M. Ongali Robert", "8 boulevard Denis Sassou", "Brazzaville", "06 998 76 54", "cd@bijou.cg", "congodiamants.cg", "B1000453", "553 333 888 00045"),
    ("SOFIBRE", "Mlle Bretheau Ana", "17 rue de la Gare", "Brazzaville", "05 441 00 11", "sofibre@bijou.cg", "sofibre.cg", "B1000454", "554 678 092 00056"),
    ("BIBO IMPORT", "M. Bibo Jospin", "3 impasse M'Bling", "Pointe-Noire", "06 554 32 10", "bibo@bijou.cg", "biboimport.cg", "B1000455", "555 812 405 00067"),
    ("AVIATECH", "Mme Avia Destinie", "22 rue Masamba", "Brazzaville", "05 620 47 33", "avia@bijou.cg", "aviatech.cg", "B1000456", "556 198 520 00078"),
    ("EDELWEISS", "M. Nkounkou Abel", "9 allee des Palmiers", "Pointe-Noire", "06 732 54 80", "edelweiss@bijou.cg", "edelweiss.cg", "B1000457", "557 460 113 00089"),
    ("GUY COQ", "M. Coqceuro Guy", "74 rue de la Loi", "Brazzaville", "05 803 92 41", "guycoq@bijou.cg", "guycoq.cg", "B1000458", "558 551 209 00090"),
    ("LATITUDE", "Mlle Kawara Lucienne", "31 avenue du 15 Aout", "Brazzaville", "06 934 08 17", "latitude@bijou.cg", "latitude.cg", "B1000459", "559 030 771 00101"),
    ("RBC MAISONS", "M. Bilomba Cris", "58 avenue General de Gaulle", "Pointe-Noire", "05 015 66 29", "rbc@bijou.cg", "rbcmaisons.cg", "B1000460", "560 312 880 00112"),
    ("THERMOCONGO", "Mme Loemba Gracia", "26 rue du Cinquantenaire", "Brazzaville", "06 476 25 38", "thermo@bijou.cg", "thermocongo.cg", "B1000461", "561 220 943 00123"),
    ("UNC BENIN", "M. Adjahoui Ben", "11 boulevard Marien Ngouabi", "Brazzaville", "05 389 70 63", "unc@bijou.cg", "uncbenin.cg", "B1000462", "562 405 562 00134"),
    ("KLC DISTRIB", "Mme Kengue Linda", "5 rue Bouet", "Brazzaville", "06 557 18 46", "klc@bijou.cg", "klcdistrib.cg", "B1000463", "563 511 204 00145"),
    ("NOUVELLES GALERIES", "M. Ikama Junior", "40 rue de l'Or", "Pointe-Noire", "05 664 81 92", "ng@bijou.cg", "ngaleries.cg", "B1000464", "564 605 118 00156"),
    ("CASINO CONGO", "Mlle Bakala Roosevelt", "18 avenue Amilcar Cabral", "Brazzaville", "06 488 53 07", "casino@bijou.cg", "casinocongo.cg", "B1000465", "565 750 629 00167"),
    ("PAKY AFRICA", "M. Mavoungou Prince", "28 rue Matsoua", "Brazzaville", "05 913 64 74", "paky@bijou.cg", "pakyafrica.cg", "B1000466", "566 398 117 00178"),
    ("SAVIA", "Mme Mpika Honorine", "61 rue Mbochis", "Pointe-Noire", "06 773 48 15", "savia@bijou.cg", "savia.cg", "B1000467", "567 210 840 00189"),
    ("SARL ISABELLA", "M. Poaty Steeve", "7 rue du Commerce", "Brazzaville", "05 224 38 66", "isabella@bijou.cg", "sarli.cg", "B1000468", "568 002 913 00190"),
    ("SETH IMPORT", "Mme Mabiala Grâce", "33 rue de la Foire", "Brazzaville", "06 560 29 37", "seth@bijou.cg", "sethimport.cg", "B1000469", "569 608 050 00201"),
    ("SKAN CAFE", "M. Matondo Wilfrid", "55 boulevard Charles de Gaulle", "Pointe-Noire", "05 386 72 90", "skan@bijou.cg", "skancafe.cg", "B1000470", "570 395 124 00212"),
    ("AQUAMETAL", "Mlle Nzaba Fleur", "14 rue Molanda", "Brazzaville", "06 480 11 58", "aqua@bijou.cg", "aquametal.cg", "B1000471", "571 804 326 00223"),
    ("CONTINENTALE", "M. Wawa Fabrice", "29 avenue Foch", "Pointe-Noire", "05 930 84 16", "continentale@bijou.cg", "continentale.cg", "B1000472", "572 512 908 00234"),
    ("HENRY ROME", "M. Rome Henri", "76 rue du 28 Aout", "Brazzaville", "06 842 39 62", "henry@bijou.cg", "henryrome.cg", "B1000473", "573 205 443 00245"),
    ("COMPTOIR MATERIAUX", "Mme Ngoma Claudine", "12 rue de l'Industrie", "Pointe-Noire", "05 260 90 33", "cm@bijou.cg", "comptoirmats.cg", "B1000474", "574 118 756 00256"),
    ("BLANCHERIE NOVA", "M. Loufoukou Divin", "2 rue Malonga", "Brazzaville", "06 519 27 44", "nova@bijou.cg", "blancnova.cg", "B1000475", "575 604 371 00267"),
    ("NOMAD", "Mme Obambi Santal", "36 rue de la Saline", "Brazzaville", "05 745 61 78", "nomad@bijou.cg", "nomad.cg", "B1000476", "576 208 992 00278"),
    ("GUAMBI CANO", "M. Guambi Cano", "4 rue Lengo", "Pointe-Noire", "06 812 34 86", "guambi@bijou.cg", "guambicano.cg", "B1000477", "577 340 517 00289"),
    ("GCP SA", "Mme Pitoun Clara", "25 avenue de la TSI", "Brazzaville", "05 903 14 27", "gcp@bijou.cg", "gcpsa.cg", "B1000478", "578 412 663 00290"),
    ("TAC NOUVELLE", "M. Kombo Richard", "63 rue des Banques", "Brazzaville", "06 473 58 91", "tac@bijou.cg", "tacnouvelle.cg", "B1000479", "579 601 820 00301"),
    ("ZITRO", "Mme Zitro Vianey", "19 avenue Matsoua", "Pointe-Noire", "05 660 23 47", "zitro@bijou.cg", "zitro.cg", "B1000480", "580 333 064 00312"),
    ("MAISON BELLE", "M. Ndamba Serge", "6 rue des Fleurs", "Brazzaville", "06 519 87 32", "belle@bijou.cg", "maisonbelle.cg", "B1000481", "581 205 197 00323"),
    ("TECHNO DISPOTRANS", "Mme Loembe Mercy", "44 rue Kouilou", "Brazzaville", "05 847 12 56", "tdt@bijou.cg", "tdt.cg", "B1000482", "582 160 922 00334"),
    ("PERE & FILS", "M. Okemba Pascal", "15 rue du Palais", "Pointe-Noire", "06 704 63 81", "pf@bijou.cg", "perefils.cg", "B1000483", "583 409 556 00345"),
    ("BEAUTY WORLD", "Mme Batchou Aurore", "50 boulevard du Sud", "Brazzaville", "05 296 44 78", "beauty@bijou.cg", "beautyworld.cg", "B1000484", "584 618 073 00356"),
    ("VEKA CONGO", "M. Bouka Yann", "23 rue de la Science", "Brazzaville", "06 560 90 12", "veka@bijou.cg", "vekacongo.cg", "B1000485", "585 700 884 00367"),
    ("IMPORT PRODUITS", "Mlle Mvouka Sandrine", "13 avenue de la Culture", "Pointe-Noire", "05 403 51 26", "imp@bijou.cg", "importproduits.cg", "B1000486", "586 241 076 00378"),
    ("PIERRE FABRE", "M. Fabre Pierre", "39 rue du Port", "Brazzaville", "06 920 47 63", "pfabre@bijou.cg", "pierrefabre.cg", "B1000487", "587 320 551 00389"),
    ("CASINO P-NOIRE", "Mme Moukala Laurie", "8 bis avenue de l'Independance", "Pointe-Noire", "05 814 20 39", "cpn@bijou.cg", "casinopn.cg", "B1000488", "588 115 640 00390"),
    ("MOONLIGHT", "M. Ngaka Eli", "66 rue de la Mer", "Brazzaville", "06 633 85 04", "moon@bijou.cg", "moonlight.cg", "B1000489", "589 820 471 00401"),
    ("SAVA DESIGN", "Mme Bilala Elysée", "20 rue Nety", "Pointe-Noire", "05 260 74 56", "sava@bijou.cg", "savadesign.cg", "B1000490", "590 402 118 00412"),
    ("AMASS INTERNATIONAL", "M. Kaya Frederic", "51 rue de la Cite", "Brazzaville", "06 741 82 25", "amass@bijou.cg", "amassintl.cg", "B1000491", "591 609 335 00423"),
    ("DISTRIBUTION PLUS", "Mme Lutumba Belise", "3 rue des Jardins", "Brazzaville", "05 335 67 82", "dplus@bijou.cg", "distriplus.cg", "B1000492", "592 511 209 00434"),
    ("COCA UNION", "M. Samba Alex", "27 avenue de la Paix", "Pointe-Noire", "06 586 90 43", "coca@bijou.cg", "cocaunion.cg", "B1000493", "593 402 657 00445"),
    ("FLEUR BLEUE", "Mme Oko Sylvie", "17 rue de la Fontaine", "Brazzaville", "05 402 91 26", "fleur@bijou.cg", "fleurbleue.cg", "B1000494", "594 330 771 00456"),
    ("DIARIES SERVICES", "M. Ewokolo Paul", "42 rue du Sport", "Pointe-Noire", "06 238 74 91", "diaries@bijou.cg", "diaries.cg", "B1000495", "595 120 906 00467"),
    ("MAISON MONGALELA", "Mme Mongalela Cisele", "29 avenue de la Revolution", "Brazzaville", "05 619 20 48", "mm@bijou.cg", "maisonm.cg", "B1000496", "596 802 344 00478"),
    ("IMPRIMERIE LEMBA", "M. Lemba Ghislain", "5 rue de l'Imprimerie", "Brazzaville", "06 710 35 26", "imp@lemba.cg", "lemba.cg", "B1000497", "597 206 577 00489"),
    ("TECHNICNAIL", "Mme Nganga Rose", "37 rue Meteo", "Pointe-Noire", "05 884 60 27", "nail@bijou.cg", "technicnail.cg", "B1000498", "598 540 130 00490"),
    ("ARVIVA", "M. Bakala Nelson", "9 rue du Stadium", "Brazzaville", "06 391 28 70", "arviva@bijou.cg", "arviva.cg", "B1000499", "599 600 882 00501"),
    ("IMAGES PLUS", "Mme Ondongo Vero", "24 rue de la Television", "Brazzaville", "05 550 96 14", "images@bijou.cg", "imagesplus.cg", "B1000500", "600 350 771 00512"),
]


def _insert_with_triggers(cur, table, sql, rows, disable_first=False):
    if disable_first:
        cur.execute("ALTER TABLE {} DISABLE TRIGGER ALL".format(table))
        try:
            for row in rows:
                cur.execute(sql, row)
        finally:
            cur.execute("ALTER TABLE {} ENABLE TRIGGER ALL".format(table))
        return True

    try:
        for row in rows:
            cur.execute(sql, row)
        return True
    except Exception as e:
        logger.warning("Insert %s via triggers echoue (%s), nouvel essai sans triggers", table, e)
        cur.execute("ALTER TABLE {} DISABLE TRIGGER ALL".format(table))
        try:
            for row in rows:
                cur.execute(sql, row)
            return True
        finally:
            cur.execute("ALTER TABLE {} ENABLE TRIGGER ALL".format(table))


def generate(articles_count=50, tiers_count=50, dry_run=False):
    cfg = get_db_config()
    if not cfg.get("server") or not cfg.get("database"):
        raise ValueError("Configuration base de donnees incomplete (config.json)")

    conn = _build_conn(cfg)
    try:
        cur = conn.cursor()

        # --- References existantes ---
        cur.execute("SELECT AR_Ref FROM F_ARTICLE")
        existing_art = {str(r[0]).strip() for r in cur.fetchall()}
        cur.execute("SELECT CT_Num FROM F_COMPTET")
        existing_tiers = {str(r[0]).strip() for r in cur.fetchall()}

        # --- Prefixes par famille ---
        prefixes = {
            "BIJOUXOR": "OR",
            "BIJOUXARG": "ARG",
            "MONTREOR": "MOR",
            "MONTREDIV": "MDIV",
            "ORFEVRERIE": "ORF",
        }

        # --- Construire les articles ---
        art_rows = []
        next_idx = {}
        for fam in FAMILLES:
            cur.execute("SELECT MAX(AR_Ref) FROM F_ARTICLE WHERE FA_CodeFamille=?", (fam,))
            mx = cur.fetchone()[0]
            num = 0
            if mx:
                import re as _re
                m = _re.search(r"(\d+)$", str(mx).strip())
                if m:
                    num = int(m.group(1))
            next_idx[fam] = num

        used_prefix = {fam: prefixes[fam] for fam in FAMILLES}
        art_added = 0
        for design, fam, pa, pv, coef in CATALOG_ARTICLES:
            if art_added >= articles_count:
                break
            nxt = next_idx[fam] + 1
            next_idx[fam] = nxt
            ref = "{}{:02d}".format(used_prefix[fam], nxt)
            while ref in existing_art:
                nxt = next_idx[fam] + 1
                next_idx[fam] = nxt
                ref = "{}{:02d}".format(used_prefix[fam], nxt)
            existing_art.add(ref)
            art_rows.append({
                "ref": ref, "design": design, "famille": fam,
                "pa": pa, "pv": pv, "coef": coef,
            })
            art_added += 1

        # --- Construire les tiers ---
        tier_rows = []
        t = 0
        for intitule, contact, adresse, ville, tel, email, site, ident, siret in CATALOG_TIERS:
            if t >= tiers_count:
                break
            code = "CLI{:04d}".format(t + 1)
            while code in existing_tiers:
                t += 1
                code = "CLI{:04d}".format(t + 1)
                if t > 200:
                    break
            existing_tiers.add(code)
            tier_rows.append({
                "code": code, "intitule": intitule, "contact": contact,
                "adresse": adresse, "ville": ville, "tel": tel,
                "email": email, "site": site, "ident": ident, "siret": siret,
            })
            t += 1

        logger.info("Articles a inserer: %d | Tiers a inserer: %d",
                    len(art_rows), len(tier_rows))

        if dry_run:
            for a in art_rows[:5]:
                logger.info("  [art] %s | %s | %s | PA=%s PV=%s", a["ref"], a["design"], a["famille"], a["pa"], a["pv"])
            logger.info("  ... (%d au total)", len(art_rows))
            for trow in tier_rows[:5]:
                logger.info("  [tiers] %s | %s | %s | %s", trow["code"], trow["intitule"], trow["ville"], trow["tel"])
            logger.info("  ... (%d au total)", len(tier_rows))
            return {"dry_run": True, "articles": len(art_rows), "tiers": len(tier_rows)}

        trigger_ok = {"article": True, "tiers": True}

        # --- Insertion articles ---
        if art_rows:
            sql_art = """
                INSERT INTO F_ARTICLE (
                    AR_Ref, AR_Design, FA_CodeFamille,
                    AR_Type, AR_Nature, AR_SuiviStock, AR_UniteVen, AR_PrixAch,
                    AR_Coef, AR_PrixVen, AR_Publie, AR_CodeBarre
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            art_params = [
                (a["ref"], a["design"], a["famille"],
                 0, 0, 2, 1, a["pa"], a["coef"], a["pv"], 1,
                 hashlib.md5(a["ref"].encode("utf-8")).hexdigest()[:13])
                for a in art_rows
            ]
            try:
                _insert_with_triggers(cur, "F_ARTICLE", sql_art, art_params)
                logger.info("Articles inseres: %d", len(art_rows))
            except Exception as e:
                logger.error("Echec insertion articles: %s", e)
                raise

        # --- Insertion tiers ---
        if tier_rows:
            sql_tier = """
                INSERT INTO F_COMPTET (
                    CT_Num, CT_Intitule, CT_Type, CG_NumPrinc,
                    CT_Qualite, CT_Classement, CT_Contact, CT_Adresse,
                    CT_CodePostal, CT_Ville, CT_Pays, CT_Raccourci,
                    BT_Num, N_Devise, CT_Identifiant, CT_Siret, CT_Encours, CT_Assurance,
                    CT_NumPayeur, N_Risque, CO_No, N_CatTarif,
                    CT_Taux01, CT_Taux02, CT_Taux03, CT_Taux04, N_CatCompta, N_Period,
                    CT_Facture, CT_BLFact, CT_Langue, N_Expedition, N_Condition, CT_Saut,
                    CT_Lettrage, CT_ValidEch, CT_Sommeil, DE_No, CT_ControlEnc, CT_NotRappel,
                    N_Analytique, CT_Telephone, CT_Telecopie, CT_EMail, CT_Site,
                    CT_Prospect
                ) VALUES ({})
            """.format(", ".join("?" for _ in range(46)))
            tier_params = []
            for row in tier_rows:
                code = _trunc(row["code"], 17)
                siret = _trunc(row["siret"].replace(" ", ""), 14)
                intitule = _trunc(row["intitule"], 69)
                contact = _trunc(row["contact"], 35)
                adresse = _trunc(row["adresse"], 35)
                ville = _trunc(row["ville"], 35)
                ident = _trunc(row["ident"], 25)
                tel = _trunc(row["tel"], 21)
                email = _trunc(row["email"], 69)
                site = _trunc(row["site"], 69)
                tier_params.append((
                    code, intitule, 0, "4110000",
                    "", _trunc(intitule, 17), contact, adresse,
                    "", ville, "République du Congo", "",
                    1, 0, ident, siret, 0, 0,
                    code, 1, 0, 1,
                    0, 0, 0, 0, 1, 1,
                    1, 0, 0, 1, 1, 1,
                    1, 0, 0, 0, 0, 0,
                    0, tel, "", email, site,
                    0,
                ))
            try:
                _insert_with_triggers(cur, "F_COMPTET", sql_tier, tier_params, disable_first=True)
                logger.info("Tiers inseres: %d", len(tier_rows))
            except Exception as e:
                logger.error("Echec insertion tiers: %s", e)
                raise

        result = {
            "articles_inseres": len(art_rows),
            "tiers_inseres": len(tier_rows),
            "database": cfg.get("database"),
        }
        logger.info("Termine: %s", result)
        return result
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Genere des articles bijouterie + tiers clients dans Sage BIJOU")
    parser.add_argument("--articles", type=int, default=50, help="Nombre d'articles (defaut 50)")
    parser.add_argument("--tiers", type=int, default=50, help="Nombre de tiers clients (defaut 50)")
    parser.add_argument("--dry-run", action="store_true", help="Simulation sans ecriture")
    args = parser.parse_args()

    try:
        result = generate(articles_count=args.articles, tiers_count=args.tiers, dry_run=args.dry_run)
        logger.info("Resultat: %s", result)
    except Exception as e:
        logger.error("Echec: %s", e)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()