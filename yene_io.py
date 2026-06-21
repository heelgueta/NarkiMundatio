"""
Yene I/O — discovers YeneConservatio SQLite archives and exposes them as
read-only article sources for the sibling analysis apps (Ñarki, Mañke,
Filu, Llallin).

A Yene source is referenced by a pseudo-filename: ``yene_<sid>.db``.
Sibling apps just need to:

  * list_yene_sources()       → entries to merge into their /files listing
  * load_yene_rows(name, ...) → CSV-row-style list[dict] for the analysis pipeline
  * is_yene_name(name)        → True if the picked "file" is a Yene source

Discovery resolution order:
  1. $YENE_DATA_DIR env var, if set
  2. ../YeneConservatio/data        (siblings live next to YeneConservatio)
  3. ../../YeneConservatio/data     (siblings live inside a wrapper repo)
"""
import os
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Columns sibling apps expect when reading a CSV row.
_ROW_COLS = ["source", "date", "title", "url", "body_text", "section"]


def find_yene_data_dir() -> Path | None:
    env = os.environ.get("YENE_DATA_DIR")
    if env:
        p = Path(env)
        if p.exists():
            return p
    for cand in (
        HERE.parent / "YeneConservatio" / "data",
        HERE.parent.parent / "YeneConservatio" / "data",
    ):
        if cand.exists():
            return cand
    return None


def is_yene_name(name: str) -> bool:
    return bool(name) and name.startswith("yene_") and name.endswith(".db")


def _sid_from_name(name: str) -> str:
    return name[len("yene_"):-len(".db")]


def list_yene_sources() -> list[dict]:
    """One entry per Yene DB, formatted like the sibling apps' file listings."""
    data_dir = find_yene_data_dir()
    if not data_dir:
        return []
    out = []
    for db_file in sorted(data_dir.glob("yene_*.db")):
        sid = db_file.stem[len("yene_"):]
        try:
            con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
            n, mn, mx = con.execute(
                "SELECT COUNT(*), MIN(date), MAX(date) FROM articles"
            ).fetchone()
            con.close()
        except Exception:
            n, mn, mx = 0, None, None
        if not n:
            continue
        out.append({
            "name":   db_file.name,                   # "yene_elpinguino.db"
            "size":   db_file.stat().st_size,
            "mtime":  db_file.stat().st_mtime,
            "source": "yene",
            "meta": {
                "source_id":  sid,
                "n_articles": n,
                "min_date":   mn,
                "max_date":   mx,
            },
        })
    return out


def load_yene_rows(name: str,
                   date_from: str = "",
                   date_to: str = "",
                   limit: int | None = None) -> list[dict]:
    """Read a Yene DB and return rows in CSV-row format."""
    if not is_yene_name(name):
        return []
    data_dir = find_yene_data_dir()
    if not data_dir:
        return []
    db_file = data_dir / name
    if not db_file.exists():
        return []

    sql = (
        "SELECT source, date, title, url, body_text, section "
        "FROM articles WHERE 1=1"
    )
    params: list = []
    if date_from:
        sql += " AND date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND date <= ?"
        params.append(date_to)
    sql += " ORDER BY date"
    if limit and limit > 0:
        sql += f" LIMIT {int(limit)}"

    con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(sql, params).fetchall()]
    con.close()
    # Normalise missing keys so downstream code that does row.get("body_text")
    # never KeyErrors.
    for r in rows:
        for c in _ROW_COLS:
            r.setdefault(c, "")
    return rows
