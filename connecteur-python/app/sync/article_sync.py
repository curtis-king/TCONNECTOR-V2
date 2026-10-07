import logging
from app.storage.db import get_cursor
from app.integration.sage.database import fetch_articles

logger = logging.getLogger("t-connector.articles")


def _upsert_products(articles):
    # 1. Normalisation (regles inchangees)
    normed = []
    for art in articles:
        ref = (art.get("ref") or "").strip()
        if not ref:
            continue
        designation = art.get("designation") or ref
        barcode = art.get("barcode") or ""
        famille = art.get("famille") or ""
        unite = art.get("unite") or "U"
        tva_code = art.get("tva_code")
        tva_code = str(tva_code).strip() if tva_code is not None else "18"
        if not tva_code or tva_code.upper() in ("NULL", "NONE"):
            tva_code = "18"
        try:
            nature = int(float(art.get("nature", 0) or 0))
        except (ValueError, TypeError):
            nature = 0
        try:
            prix_vente = float(art.get("prix_vente", 0) or 0)
        except (ValueError, TypeError):
            prix_vente = 0.0
        try:
            prix_achat = float(art.get("prix_achat", 0) or 0)
        except (ValueError, TypeError):
            prix_achat = 0.0
        try:
            stock = float(art.get("stock", 0) or 0)
        except (ValueError, TypeError):
            stock = 0.0
        try:
            est_actif = int(float(art.get("est_actif", 1) or 0))
        except (ValueError, TypeError):
            est_actif = 1
        if est_actif not in (0, 1):
            est_actif = 1
        normed.append((ref, barcode, designation, famille, nature, prix_vente,
                       prix_achat, tva_code, unite, stock, est_actif))
    if not normed:
        return 0, 0

    # 2. Prechargement existants par chunks de 500 (les 2 cotes du OR d'origine)
    existing = {}
    refs = list({n[0] for n in normed})
    with get_cursor() as cur:
        for i in range(0, len(refs), 500):
            chunk = refs[i:i + 500]
            q = ",".join(["?"] * len(chunk))
            cur.execute(
                "SELECT id, ref, sage_ar_ref FROM products WHERE ref IN ({}) OR sage_ar_ref IN ({})".format(q, q),
                chunk + chunk)
            for row in cur.fetchall():
                existing[row["ref"]] = row["id"]
                if row["sage_ar_ref"]:
                    existing[row["sage_ar_ref"]] = row["id"]

        # 3. Split + executemany (memes SQL que l'original)
        to_update, to_insert = [], []
        for n in normed:
            ref = n[0]
            if ref in existing:
                to_update.append((n[1], n[2], n[3], n[4], n[5], n[6], n[7], n[8], n[9], n[10], ref, existing[ref]))
            else:
                to_insert.append((ref,) + n[1:])
        updated = inserted = 0
        if to_update:
            cur.executemany("""
                UPDATE products SET
                    barcode = ?, designation = ?, famille = ?,
                    nature = ?, prix_vente = ?, prix_achat = ?,
                    tva_code = ?, unite = ?, stock_reel = ?,
                    est_actif = ?, synced_sage = 1, sage_ar_ref = ?,
                    updated_at = datetime('now')
                WHERE id = ?
            """, to_update)
            updated = len(to_update)
        if to_insert:
            cur.executemany("""
                INSERT OR IGNORE INTO products (
                    ref, barcode, designation, famille, nature,
                    prix_vente, prix_achat, tva_code, unite, stock_reel,
                    est_actif, synced_sage, sage_ar_ref
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """, to_insert)
            rc = cur.rowcount
            inserted = rc if (rc is not None and rc >= 0) else len(to_insert)

    return inserted, updated

def sync_articles_from_sage():
    try:
        articles = fetch_articles()
    except Exception as e:
        logger.error("Sync articles: lecture SQL Server echouee: %s", e)
        return {"ok": False, "error": str(e), "inserted": 0, "updated": 0, "total": 0}

    if not articles:
        logger.warning("Sync articles: aucune article retourne par F_ARTICLE")
        return {"ok": True, "inserted": 0, "updated": 0, "total": 0,
                "notice": "Aucun article dans F_ARTICLE (SQL Server Sage)"}

    try:
        inserted, updated = _upsert_products(articles)
    except Exception as e:
        logger.error("Sync articles: ecriture SQLite echouee: %s", e)
        return {"ok": False, "error": str(e), "inserted": 0, "updated": 0, "total": len(articles)}

    logger.info("Sync articles terminee: %d inseres, %d mis a jour (sur %d)",
                inserted, updated, len(articles))
    return {"ok": True, "inserted": inserted, "updated": updated, "total": len(articles)}