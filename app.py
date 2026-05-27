"""
ÑarkiMundatio — text cleaning and standardisation.

ñarki (cat, mapudungun) + mundatio (cleansing, latin)

Reads CSVs from CulpemCorpus output/, cleans body_text and title,
optionally splits articles into labelled sentences with verse IDs,
exports cleaned CSV + JSON metadata.
"""

import csv
import datetime
import hashlib
import json
import os
import re
import unicodedata
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file
from flask import stream_with_context

app = Flask(__name__)
HERE       = Path(__file__).parent.resolve()
OUTPUT_DIR = HERE / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

ARTICLE_FIELDS = ["source", "date", "title", "url", "body_text", "section"]


def find_culpem_output() -> Path | None:
    candidates = [
        HERE.parent / "CulpemCorpus" / "output",
        HERE.parent.parent / "CulpemCorpus" / "output",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def find_yene_jobs() -> list[dict]:
    """Read YeneConservatio job JSONs to show archive coverage."""
    candidates = [
        HERE.parent / "YeneConservatio" / "data" / "jobs",
        HERE.parent.parent / "YeneConservatio" / "data" / "jobs",
    ]
    jobs = []
    for d in candidates:
        if d.exists():
            for f in sorted(d.glob("yene_*.json"),
                            key=lambda x: x.stat().st_mtime, reverse=True):
                try:
                    with open(f, encoding="utf-8") as fh:
                        jobs.append(json.load(fh))
                except Exception:
                    pass
            break
    return jobs


# ── cleaning helpers ──────────────────────────────────────────────────────────

def remove_accents(text: str) -> str:
    nfd = unicodedata.normalize("NFD", text)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


def clean_text(text: str, opts: dict) -> str:
    if opts.get("remove_html"):
        text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)
    if opts.get("remove_junk"):
        text = re.sub(r"https?://\S+", "", text)
        text = re.sub(r"\S+@\S+\.\S+", "", text)
        text = re.sub(r"[|]{2,}", " ", text)
        text = re.sub(r"[-_=]{4,}", " ", text)
        text = re.sub(r"\s*\[…?\]\s*", " ", text)
    if opts.get("clean_punct"):
        # remove decorative quotes, brackets, ornamental chars
        text = re.sub(r'[""«»„‟\[\]{}\(\)]', "", text)
        # collapse ellipsis variants
        text = re.sub(r"\.{2,}|…", ".", text)
        # remove stray asterisks and pipes
        text = re.sub(r"[*|#~^]", "", text)
    if opts.get("remove_accents"):
        text = remove_accents(text)
    if opts.get("replace_n"):
        text = text.replace("ñ", "n").replace("Ñ", "N")
    if opts.get("lowercase"):
        text = text.lower()
    text = re.sub(r" {2,}", " ", text).strip()
    return text


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+", text)
    return [p.strip() for p in parts if len(p.strip()) > 15]


def article_hash(row: dict) -> str:
    key = row.get("url") or row.get("title") or str(row)
    return hashlib.sha1(key.encode("utf-8", errors="replace")).hexdigest()[:8]


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/files")
def files():
    culpem_dir = find_culpem_output()
    result = []
    if culpem_dir:
        for f in sorted(culpem_dir.glob("culpem_*.csv"),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            meta = {}
            mp = f.with_suffix(".json")
            if mp.exists():
                with open(mp, encoding="utf-8") as mf:
                    meta = json.load(mf)
            result.append({
                "name":  f.name,
                "size":  f.stat().st_size,
                "mtime": f.stat().st_mtime,
                "meta":  meta,
            })
    return jsonify({"files": result,
                    "culpem_dir": str(culpem_dir) if culpem_dir else None})


@app.route("/narki_files")
def narki_files():
    result = []
    for f in sorted(OUTPUT_DIR.glob("narki_*.csv"),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        meta = {}
        mp = f.with_suffix(".json")
        if mp.exists():
            with open(mp, encoding="utf-8") as mf:
                meta = json.load(mf)
        result.append({
            "name":  f.name,
            "size":  f.stat().st_size,
            "mtime": f.stat().st_mtime,
            "meta":  meta,
        })
    return jsonify({"files": result})


@app.route("/yene_coverage")
def yene_coverage():
    jobs = find_yene_jobs()
    # Summarise: latest job per source
    latest = {}
    for j in jobs:
        sid = j.get("source_id")
        if sid and sid not in latest:
            latest[sid] = j
    return jsonify(list(latest.values()))


@app.route("/process")
def process():
    filename = request.args.get("file", "").strip()
    mode     = request.args.get("mode", "article")   # "article" | "phrase"
    opts = {
        "remove_html":    request.args.get("remove_html",    "1") == "1",
        "remove_junk":    request.args.get("remove_junk",    "1") == "1",
        "clean_punct":    request.args.get("clean_punct",    "0") == "1",
        "remove_accents": request.args.get("remove_accents", "0") == "1",
        "replace_n":      request.args.get("replace_n",      "0") == "1",
        "lowercase":      request.args.get("lowercase",      "0") == "1",
    }

    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "Invalid filename"}), 400

    culpem_dir = find_culpem_output()
    if not culpem_dir:
        return jsonify({"error": "CulpemCorpus output not found"}), 404

    filepath = culpem_dir / filename
    if not filepath.exists():
        return jsonify({"error": "File not found"}), 404

    def generate():
        yield _sse({"type": "log", "level": "info", "msg": f"Leyendo {filename}…"})

        try:
            with open(filepath, encoding="utf-8-sig", newline="") as f:
                all_rows = list(csv.DictReader(f))
        except Exception as e:
            yield _sse({"type": "log", "level": "error", "msg": f"Error al leer: {e}"})
            yield _sse({"type": "done", "count": 0, "file": None})
            return

        total = len(all_rows)
        yield _sse({"type": "log", "level": "info",
                    "msg": f"{total} artículos. Modo: {mode}. Opciones: {opts}"})

        out_rows = []

        for i, row in enumerate(all_rows):
            cleaned = dict(row)
            for field in ("title", "body_text"):
                if field in cleaned:
                    cleaned[field] = clean_text(cleaned.get(field) or "", opts)

            if mode == "phrase":
                aid   = article_hash(row)
                src   = (row.get("source") or "").strip()
                date  = (row.get("date")   or "")[:10]

                # sentence 0 = title
                title_text = cleaned.get("title", "").strip()
                if title_text:
                    tr = {k: cleaned.get(k, "") for k in ARTICLE_FIELDS}
                    tr["body_text"]  = title_text
                    tr["is_title"]   = "1"
                    tr["sentence_n"] = "0"
                    tr["verse_id"]   = f"{src}_{date}_{aid}:0000"
                    out_rows.append(tr)

                # sentences 1..N = body sentences
                for n, phrase in enumerate(
                        split_sentences(cleaned.get("body_text", "")), start=1):
                    pr = {k: cleaned.get(k, "") for k in ARTICLE_FIELDS}
                    pr["body_text"]  = phrase
                    pr["is_title"]   = "0"
                    pr["sentence_n"] = str(n)
                    pr["verse_id"]   = f"{src}_{date}_{aid}:{n:04d}"
                    out_rows.append(pr)
            else:
                out_rows.append(cleaned)

            if (i + 1) % 100 == 0 or i == total - 1:
                yield _sse({"type": "progress", "done": i + 1, "total": total})

        if not out_rows:
            yield _sse({"type": "log", "level": "warn", "msg": "Sin filas de salida."})
            yield _sse({"type": "done", "count": 0, "file": None})
            return

        job_id  = uuid.uuid4().hex[:8]
        stem    = re.sub(r"^culpem_", "", filename.rsplit(".", 1)[0])
        suffix  = "_frases" if mode == "phrase" else ""
        out_name = f"narki_{stem}{suffix}_{job_id}.csv"
        out_path = OUTPUT_DIR / out_name

        fields = list(out_rows[0].keys())
        with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(out_rows)

        meta = {
            "created_at":  datetime.datetime.utcnow().isoformat() + "Z",
            "source_file": filename,
            "mode":        mode,
            "phrase_split": mode == "phrase",
            "options":     opts,
            "n_input":     total,
            "n_output":    len(out_rows),
            "extra_columns": (["is_title", "sentence_n", "verse_id"]
                               if mode == "phrase" else []),
            "file":        out_name,
        }
        with open(OUTPUT_DIR / out_name.replace(".csv", ".json"),
                  "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        yield _sse({"type": "log", "level": "ok",
                    "msg": f"✓ Listo — {len(out_rows)} filas → {out_name}"})
        yield _sse({"type": "done", "count": len(out_rows), "file": out_name})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/download/<filename>")
def download(filename):
    if not filename.startswith("narki_") or ".." in filename or "/" in filename:
        return "Not found", 404
    if not filename.endswith((".csv", ".json")):
        return "Not found", 404
    fp = OUTPUT_DIR / filename
    if not fp.exists():
        return "Not found", 404
    return send_file(str(fp), as_attachment=True)


def _sse(data):
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


if __name__ == "__main__":
    app.run(debug=True, port=5004, threaded=True)
