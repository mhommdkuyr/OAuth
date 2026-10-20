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
FIRST = ["name","phone","email","website","category","address","rating","review_count",
         "google_maps_url","latitude","longitude","place_id","source_engine","opening_hours"]
EMPTY = {"", "none", "null", "nan", "n/a", "na"}
PLUS_CODE_RE = re.compile(r"(?<![A-Z0-9])([23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,3})(?![A-Z0-9])", re.I)

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
    # Arabic variants commonly used in business listings.
    s = s.translate(str.maketrans({"أ":"ا","إ":"ا","آ":"ا","ى":"ي","ة":"ه"}))
    return " ".join(re.sub(r"[^\w]+", " ", s, flags=re.UNICODE).split())

def row_keys(row, index):
    """Return multiple independent keys so an ID-bearing row can match one without that ID."""
    keys = set()
    name = cleankey(row.get("name") or row.get("title"))
    if not name:
        return {"unidentified:" + str(index)}

    for field in ("place_id", "cid", "data_id"):
        value = row.get(field)
        if good(value):
            keys.add("id:" + cleankey(value))

    url = row.get("google_maps_url") or row.get("link")
    if good(url):
        url = re.sub(r"[?#].*$", "", str(url).strip()).rstrip("/").casefold()
        if url:
            keys.add("url:" + url)

    phone = re.sub(r"\D+", "", str(row.get("phone") or ""))
    if len(phone) >= 7:
        keys.add("name-phone:" + name + "|" + phone)

    address = str(row.get("address") or "")
    addr_key = cleankey(address)
    if addr_key:
        keys.add("name-address:" + name + "|" + addr_key)
        for match in PLUS_CODE_RE.finditer(address):
            code = cleankey(match.group(1))
            keys.add("name-pluscode:" + name + "|" + code)

    try:
        lat, lon = float(row.get("latitude")), float(row.get("longitude"))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            keys.add(f"name-coords:{name}|{lat:.4f}|{lon:.4f}")
    except (TypeError, ValueError):
        pass

    category = cleankey(row.get("category"))
    # Only use this weak fallback if the row has no usable location or contact information.
    strong_prefixes = ("id:", "url:", "name-phone:", "name-address:", "name-pluscode:", "name-coords:")
    if category and not any(k.startswith(strong_prefixes) for k in keys):
        keys.add("name-category:" + name + "|" + category)
    return keys or {"unidentified:" + str(index)}

def merge_records(previous, current):
    """Merge rows using union-find across every matching candidate key; returns rows and counts."""
    records = [normalize(r) for r in previous] + [normalize(r) for r in current]
    old_count = len(previous)
    parent = list(range(len(records)))
    rank = [0] * len(records)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        if rank[ra] == rank[rb]:
            rank[ra] += 1

    key_owner = {}
    for i, row in enumerate(records):
        for candidate in row_keys(row, i):
            if candidate in key_owner:
                union(i, key_owner[candidate])
            else:
                key_owner[candidate] = i

    groups = {}
    for i in range(len(records)):
        groups.setdefault(find(i), []).append(i)

    merged_rows, new_unique = [], 0
    for members in sorted(groups.values(), key=lambda m: m[0]):
        combined, sources = {}, set()
        for i in members:
            row = records[i]
            source = str(row.get("source_engine") or "").strip()
            if source and source.casefold() not in EMPTY:
                sources.update(s.strip() for s in re.split(r"[;,]", source) if s.strip())
            for field, value in row.items():
                if field == "source_engine" or not good(value):
                    continue
                # Preserve the first non-empty canonical value, but fill blanks with later data.
                if not good(combined.get(field)):
                    combined[field] = value
        combined["source_engine"] = ";".join(sorted(sources, key=str.casefold)) if sources else "unknown"
        merged_rows.append(combined)
        if any(i >= old_count for i in members) and not any(i < old_count for i in members):
            new_unique += 1

    return merged_rows, len(records) - len(merged_rows), new_unique

def as_cell(v):
    return json.dumps(v, ensure_ascii=False, separators=(",", ":")) if isinstance(v, (dict,list,tuple)) else ("" if v is None else str(v))

def atomic_text(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

def main():
    previous = read_previous()
    a, b = read_csv(S1, "mahanaicoach"), read_csv(S2, "gosom")
    rows, collapsed, added = merge_records(previous, [*a, *b])
    columns = list(FIRST)
    extras = sorted(set().union(*(r.keys() for r in rows)) - set(columns)) if rows else []
    columns += extras
    atomic_text(OUT_JSON, json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    tmp = OUT_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: as_cell(r.get(k, "")) for k in columns})
    os.replace(tmp, OUT_CSV)
    report = {
        "run_id": os.getenv("GITHUB_RUN_ID", "local"),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "city": "Ibb, Yemen",
        "grid_size": os.getenv("GRID_SIZE", "4"),
        "previous_database_rows": len(previous),
        "mahanaicoach_rows_this_run": len(a),
        "gosom_rows_this_run": len(b),
        "current_run_rows_before_deduplication": len(a) + len(b),
        "new_unique_rows_added": added,
        "final_unique_rows": len(rows),
        "duplicate_rows_collapsed_in_this_merge": collapsed,
        "coverage_note": "Best-effort collection, not a certified complete census. Search limits, geocoding boundaries, rate limits, query language and changing listings may omit places."
    }
    # Field-level completeness metrics make it possible to distinguish row count from data quality.
    total = len(rows)
    measured_fields = [
        "name", "phone", "email", "website", "category", "address", "rating", "review_count",
        "google_maps_url", "latitude", "longitude", "place_id", "plus_code", "opening_hours",
        "price_range", "reviews_per_rating", "reviews_link", "user_reviews", "user_reviews_extended",
        "images", "thumbnail", "street_view_url", "descriptions", "about", "credit_cards_accepted",
        "owner", "menu", "order_online", "reservations", "instagram", "facebook", "linkedin",
    ]
    coverage = {}
    for field in measured_fields:
        filled = sum(1 for row in rows if good(row.get(field)))
        coverage[field] = {
            "filled": filled,
            "total": total,
            "percent": round(100 * filled / total, 2) if total else 0.0,
        }
    report["field_coverage"] = coverage
    report["output_column_count"] = len(columns)
    report["additional_fields_beyond_eight_core"] = max(0, len(columns) - 8)
    report["records_with_all_eight_core_fields"] = sum(
        1 for row in rows if all(good(row.get(field)) for field in
                                  ("name","phone","email","website","category","address","rating","review_count"))
    )
    report["collection_method"] = "Two sequential Google Maps scraper engines; website visits are used for public email/social-link enrichment; no proxy rotation configured."
    atomic_text(REPORT, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
