import hashlib
import logging
from sqlite_db import get_cursor
from database import fetch_articles

logger = logging.getLogger("t-connector.articles")


def _generate_barcode(ref):
    h = hashlib.md5(ref.encode("utf-8")).hexdigest()[:13]
    return h


def _upsert_products(articles):
    inserted = 0
    updated = 0
    with get_cursor() as cur:
        for art in articles:
            ref = (art.get("ref") or "").strip()
            if not ref:
                continue

            designation = art.get("designation") or ref
            barcode = art.get("barcode") or ""
            if not barcode:
                barcode = _generate_barcode(ref)
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

            cur.execute(
                "SELECT id, barcode FROM products WHERE ref = ? OR sage_ar_ref = ?",
                (ref, ref)
            )
            row = cur.fetchone()

            if row:
                existing_bc = (row["barcode"] or "").strip()
                final_barcode = barcode if barcode else (existing_bc or _generate_barcode(ref))
                cur.execute("""
                    UPDATE products SET
                        barcode = ?, designation = ?, famille = ?,
                        nature = ?, prix_vente = ?, prix_achat = ?,
                        tva_code = ?, unite = ?, stock_reel = ?,
                        est_actif = ?, synced_sage = 1, sage_ar_ref = ?,
                        updated_at = datetime('now')
                    WHERE id = ?
                """, (
                    final_barcode, designation, famille, nature, prix_vente, prix_achat,
                    tva_code, unite, stock, est_actif, ref, row["id"]
                ))
                updated += 1
            else:
                cur.execute("""
                    INSERT OR IGNORE INTO products (
                        ref, barcode, designation, famille, nature,
                        prix_vente, prix_achat, tva_code, unite, stock_reel,
                        est_actif, synced_sage, sage_ar_ref
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """, (
                    ref, barcode, designation, famille, nature,
                    prix_vente, prix_achat, tva_code, unite, stock,
                    est_actif, ref
                ))
                if cur.rowcount > 0:
                    inserted += 1

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