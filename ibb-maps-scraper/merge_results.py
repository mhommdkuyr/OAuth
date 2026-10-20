#!/usr/bin/env python3
"""Merge two scraper CSVs into a normalized, deduplicated database. Standard library only."""
import csv, json, os, re, sys, unicodedata
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
S1, S2 = ROOT / "ibb_skill1_results.csv", ROOT / "ibb_skill2_results.csv"
OUT_JSON, OUT_CSV, REPORT = (ROOT / "ibb_city_complete_database.json",
                             ROOT / "ibb_city_complete_database.csv",
                             ROOT / "scrape_report.json")
ALIASES = {
    "name": ("name", "title", "business_name"),
    "category": ("category", "type", "primary_type"),
    "address": ("address", "complete_address", "formatted_address"),
    "phone": ("phone", "phone_number", "telephone"),
    "email": ("email", "emails", "e_mail"),
    "website": ("website", "site", "website_url"),
    "google_maps_url": ("google_maps_url", "maps_url", "link", "url"),
    "latitude": ("latitude", "lat"), "longitude": ("longitude", "lon", "lng"),
    "rating": ("rating", "review_rating", "stars"),
    "review_count": ("review_count", "reviews_count", "reviews"),
    "opening_hours": ("opening_hours", "open_hours", "hours"),
    "place_id": ("place_id", "google_place_id", "cid", "data_id"),
}
FIRST = ["name","category","address","phone","email","website","google_maps_url",
         "latitude","longitude","rating","review_count","opening_hours","place_id","source_engine"]
EMPTY = {"", "none", "null", "nan", "n/a", "na"}

def good(v):
    return v is not None and (not isinstance(v, str) or v.strip().casefold() not in EMPTY)

def normalize(row):
    row = dict(row)
    for canon, aliases in ALIASES.items():
        value = next((row.get(k) for k in aliases if good(row.get(k))), "")
        if not good(row.get(canon)):
            row[canon] = value
    for k in ALIASES:
        row.setdefault(k, "")
    row.setdefault("source_engine", "unknown")
    return row

def read_csv(path, source):
    if not path.exists() or not path.stat().st_size:
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    result = []
    for raw in rows:
        if any(good(v) for v in raw.values()):
            row = normalize(raw)
            row.setdefault("source_engine", source)
            if not good(row.get("source_engine")) or row.get("source_engine") == "unknown":
                row["source_engine"] = source
            result.append(row)
    return result

def read_previous():
    if not OUT_JSON.exists() or not OUT_JSON.stat().st_size:
        return []
    try:
        rows = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        return [normalize(r) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: existing database not readable: {exc}", file=sys.stderr)
        return []

def cleankey(value):
    s = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    s = re.sub(r"[\u064B-\u065F\u0670]", "", s)
    return " ".join(re.sub(r"[^\w]+", " ", s, flags=re.UNICODE).split())

def key(row):
    for k in ("place_id", "cid", "data_id"):
        if good(row.get(k)):
            return "id:" + cleankey(row[k])
    if good(row.get("google_maps_url")):
        return "url:" + str(row["google_maps_url"]).strip().rstrip("/").casefold()
    name, addr = cleankey(row.get("name") or row.get("title")), cleankey(row.get("address"))
    phone = re.sub(r"\D+", "", str(row.get("phone") or ""))
    if name and addr: return "name-address:" + name + "|" + addr
    if name and phone: return "name-phone:" + name + "|" + phone
    try:
        if name: return f"name-coords:{name}|{float(row['latitude']):.4f}|{float(row['longitude']):.4f}"
    except (KeyError, TypeError, ValueError): pass
    if name: return "name-category:" + name + "|" + cleankey(row.get("category"))
    return "unidentified:" + str(id(row))

def merge(groups):
    by_key, collapsed = {}, 0
    for raw in groups:
        row, k = normalize(raw), key(normalize(raw))
        if k not in by_key:
            by_key[k] = row
        else:
            collapsed += 1
            for field, value in row.items():
                if good(value): by_key[k][field] = value
    return list(by_key.values()), collapsed

def as_cell(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":")) if isinstance(v, (dict,list,tuple)) else ("" if v is None else str(v))

def atomic_text(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

def main():
    previous = read_previous()
    a, b = read_csv(S1, "mahanaicoach"), read_csv(S2, "gosom")
    old_keys = {key(r) for r in previous}
    rows, collapsed = merge([*previous, *a, *b])
    added = len({key(r) for r in rows} - old_keys)
    columns = list(FIRST)
    extras = sorted(set().union(*(r.keys() for r in rows)) - set(columns)) if rows else []
    columns += extras
    atomic_text(OUT_JSON, json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    tmp = OUT_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows: w.writerow({k: as_cell(r.get(k, "")) for k in columns})
    os.replace(tmp, OUT_CSV)
    report = {
        "run_id": os.getenv("GITHUB_RUN_ID", "local"),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "city": "Ibb, Yemen", "grid_size": os.getenv("GRID_SIZE", "4"),
        "previous_database_rows": len(previous),
        "mahanaicoach_rows_this_run": len(a), "gosom_rows_this_run": len(b),
        "current_run_rows_before_deduplication": len(a) + len(b),
        "new_unique_rows_added": added, "final_unique_rows": len(rows),
        "duplicate_rows_collapsed_in_this_merge": collapsed,
        "coverage_note": "Best-effort collection, not a certified complete census. Search limits, geocoding boundaries, rate limits, query language and changing listings may omit places."
    }
    atomic_text(REPORT, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
