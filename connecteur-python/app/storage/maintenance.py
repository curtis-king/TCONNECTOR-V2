"""Maintenance de la base SQLite locale (proposition 09 §5).

Aucune de ces operations ne touche Sage ni le miroir fiscal distant. Le
script `scripts/maintain_db.py` est en dry-run par defaut ; `--apply` est
obligatoire pour archiver/purger, et `--vacuum` pour compacter.
"""
import csv
import gzip
import os
import sqlite3
from datetime import datetime, timezone

from app.storage import db as sqlite_db

DEFAULT_RETENTION_DAYS = 90


def _connect(db_path=None):
    path = db_path or sqlite_db.DB_PATH
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _cutoff(days):
    return "-{} days".format(max(1, int(days)))


def db_size_bytes(db_path=None):
    path = db_path or sqlite_db.DB_PATH
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def count_old_sync_log(days=DEFAULT_RETENTION_DAYS, db_path=None):
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sync_log WHERE synced_at < datetime('now', ?)",
            (_cutoff(days),),
        ).fetchone()
        return int(row["n"] or 0)
    finally:
        conn.close()


def archive_sync_log(days=DEFAULT_RETENTION_DAYS, archive_path=None, db_path=None):
    """Exporte les lignes anciennes en CSV gzip. Retourne (chemin, lignes)."""
    conn = _connect(db_path)
    if archive_path is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive_path = os.path.join(
            os.path.dirname(db_path or sqlite_db.DB_PATH),
            "sync_log-{}.csv.gz".format(stamp),
        )
    os.makedirs(os.path.dirname(os.path.abspath(archive_path)), exist_ok=True)
    count = 0
    try:
        cursor = conn.execute(
            "SELECT id, table_name, record_id, record_numero, action, direction, "
            "status, error, synced_at FROM sync_log "
            "WHERE synced_at < datetime('now', ?) ORDER BY id",
            (_cutoff(days),),
        )
        with gzip.open(archive_path, "wt", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow([
                "id", "table_name", "record_id", "record_numero", "action",
                "direction", "status", "error", "synced_at",
            ])
            for row in cursor:
                writer.writerow([row[k] for k in row.keys()])
                count += 1
        try:
            os.chmod(archive_path, 0o600)
        except OSError:
            pass
    finally:
        conn.close()
    return archive_path, count


def purge_sync_log(days=DEFAULT_RETENTION_DAYS, apply=False, archive_path=None,
                   db_path=None):
    """Archive puis supprime les lignes de plus de `days` jours.

    En dry-run, aucune ligne n'est supprimee et aucune archive n'est creee.
    """
    old_count = count_old_sync_log(days, db_path)
    result = {
        "days": int(days),
        "matched": old_count,
        "archived": 0,
        "archive_path": "",
        "deleted": 0,
        "applied": bool(apply),
    }
    if not apply or old_count == 0:
        return result

    path, archived = archive_sync_log(days, archive_path, db_path)
    result["archive_path"] = path
    result["archived"] = archived
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM sync_log WHERE synced_at < datetime('now', ?)",
            (_cutoff(days),),
        )
        result["deleted"] = int(cur.rowcount or 0)
    finally:
        conn.close()
    return result


def checkpoint(db_path=None):
    conn = _connect(db_path)
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return {"ok": True, "result": list(row) if row else []}
    finally:
        conn.close()


def vacuum(db_path=None):
    conn = _connect(db_path)
    try:
        conn.execute("VACUUM")
        return {"ok": True, "size_bytes": db_size_bytes(db_path)}
    finally:
        conn.close()


def run_maintenance(days=DEFAULT_RETENTION_DAYS, apply=False,
                    vacuum_db=False, archive_path=None, db_path=None):
    """Orchestration complete, utilisee par le script et les tests."""
    before = db_size_bytes(db_path)
    result = purge_sync_log(days, apply=apply, archive_path=archive_path,
                            db_path=db_path)
    result["size_before"] = before
    result["size_after"] = before
    if apply:
        result["checkpoint"] = checkpoint(db_path)
        if vacuum_db:
            result["vacuum"] = vacuum(db_path)
            result["size_after"] = db_size_bytes(db_path)
    return result
