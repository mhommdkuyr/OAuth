#!/usr/bin/env python3
"""Resumable, conservative, multi-pass Ibb business census.

One invocation handles a small tile batch so GitHub-hosted job limits do not discard progress.
State and successful tile CSVs are committed after every workflow batch. Uses public endpoints only;
no proxy rotation or access-control bypass is configured.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "deep_scan_state.json"
TILES_ROOT = ROOT / "deep_scan_tiles"
QUERY_PATH = ROOT / "queries_ibb.txt"
DB_JSON = ROOT / "ibb_city_complete_database.json"
S1 = ROOT / "ibb_skill1_results.csv"
S2 = ROOT / "ibb_skill2_results.csv"
OSM_CSV = ROOT / "ibb_osm_results.csv"
API_BASE = os.getenv("SCRAPER_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
API_KEY = os.getenv("SCRAPER_API_KEY", "")
UA = "IbbCensusBot/1.0 (+https://github.com/mhommdkuyr/OAuth)"
# Several increasingly dense passes; the cursor is persisted by grid-size and tile number.
GRID_STAGES = [10, 15, 20, 25]
MAX_ATTEMPTS = 3
DEFAULT_TARGET = 10_000
RAW_FIELDS = [
    "input_id","link","title","category","address","open_hours","popular_times",
    "website","phone","plus_code","review_count","review_rating","reviews_per_rating",
    "latitude","longitude","cid","status","descriptions","reviews_link","thumbnail",
    "timezone","price_range","data_id","street_view_url","place_id","images",
    "reservations","order_online","menu","owner","complete_address",
    "credit_cards_accepted","about","user_reviews","user_reviews_extended","emails",
    "instagram","facebook","linkedin","scan_grid_size","scan_tile_index","scan_completed_at_utc"
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_state(state: dict[str, Any]) -> None:
    state["updated_at_utc"] = utc_now()
    atomic_write(STATE_PATH, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def default_state(target: int) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "city": "Ibb, Yemen",
        "bbox": None,
        "grid_stages": GRID_STAGES,
        "stage_index": 0,
        "next_tile_index": 0,
        "completed_tiles": {},
        "attempts": {},
        "failed_tiles": {},
        "overpass_complete": False,
        "overpass_last_error": "",
        "gosom_complete": False,
        "target_unique_records": target,
        "target_reached": False,
        "scan_complete": False,
        "scan_status": "initializing",
        "batch_runs": 0,
        "last_batch_tiles": [],
        "last_batch_rows": 0,
        "last_batch_errors": [],
        "started_at_utc": utc_now(),
        "updated_at_utc": utc_now(),
    }


def load_state(target: int) -> dict[str, Any]:
    if not STATE_PATH.exists():
        state = default_state(target)
        save_state(state)
        return state
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("State is not a JSON object")
    except Exception as exc:
        # Do not overwrite corrupt state silently; surface a clear error for investigation.
        raise RuntimeError(f"Cannot read {STATE_PATH.name}; preserving file for recovery: {exc}") from exc
    state.setdefault("schema_version", 1)
    state.setdefault("grid_stages", GRID_STAGES)
    state.setdefault("stage_index", 0)
    state.setdefault("next_tile_index", 0)
    state.setdefault("completed_tiles", {})
    state.setdefault("attempts", {})
    state.setdefault("failed_tiles", {})
    state.setdefault("overpass_complete", False)
    state.setdefault("overpass_last_error", "")
    state.setdefault("gosom_complete", False)
    state.setdefault("target_unique_records", target)
    state.setdefault("target_reached", False)
    state.setdefault("scan_complete", False)
    state.setdefault("scan_status", "resuming")
    state.setdefault("batch_runs", 0)
    state.setdefault("last_batch_tiles", [])
    state.setdefault("last_batch_rows", 0)
    state.setdefault("last_batch_errors", [])
    return state


def http_request(url: str, data: bytes | None = None, headers: dict[str, str] | None = None,
                 timeout: int = 60, attempts: int = 3) -> bytes:
    base_headers = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        base_headers.update(headers)
    last: Exception | None = None
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=data, headers=base_headers,
                                     method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (400, 401, 403, 404):
                raise
            if attempt + 1 < attempts:
                time.sleep(min(5 * (attempt + 1), 15))
        except Exception as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(min(5 * (attempt + 1), 15))
    raise RuntimeError(f"Request failed after {attempts} attempts: {url}: {last}")


def geocode_bbox() -> list[float]:
    q = urllib.parse.urlencode({"format": "jsonv2", "limit": "1", "q": "Ibb, Yemen"})
    raw = http_request("https://nominatim.openstreetmap.org/search?" + q, timeout=30)
    hits = json.loads(raw.decode("utf-8"))
    time.sleep(1.1)  # respect the public Nominatim service policy
    if not hits or not hits[0].get("boundingbox"):
        raise RuntimeError("Nominatim did not return a bounding box for Ibb, Yemen")
    # Nominatim order: south, north, west, east.
    bbox = [float(x) for x in hits[0]["boundingbox"]]
    if len(bbox) != 4 or not (bbox[0] < bbox[1] and bbox[2] < bbox[3]):
        raise RuntimeError(f"Invalid Ibb bounding box: {bbox}")
    return bbox


def grid_cells(bbox: list[float], n: int) -> list[dict[str, Any]]:
    south, north, west, east = bbox
    dlat, dlon = (north - south) / n, (east - west) / n
    metres_per_degree = 6371000 * math.pi / 180
    cells = []
    for i in range(n):
        for j in range(n):
            lat = south + (i + 0.5) * dlat
            lon = west + (j + 0.5) * dlon
            width = max(1.0, dlon * metres_per_degree * math.cos(math.radians(lat)))
            height = dlat * metres_per_degree
            diagonal = math.hypot(height, width)
            zoom = round(math.log2(156543.03 * math.cos(math.radians(lat)) * 1000 / width))
            cells.append({
                "lat": f"{lat:.6f}",
                "lon": f"{lon:.6f}",
                # The Maps engine enforces a ~1 km radius floor; this is not 40 m coverage.
                "radius": max(1000, math.ceil(1.15 * diagonal / 2)),
                "zoom": min(17, max(11, zoom)),
            })
    return cells


def read_queries() -> list[str]:
    if not QUERY_PATH.exists():
        raise RuntimeError(f"Missing query list: {QUERY_PATH}")
    seen, queries = set(), []
    for raw in QUERY_PATH.read_text(encoding="utf-8-sig").splitlines():
        q = raw.strip()
        if q and not q.startswith("#") and q.casefold() not in seen:
            seen.add(q.casefold())
            queries.append(q)
    if not queries:
        raise RuntimeError("The query file is empty")
    return queries


def csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or not path.stat().st_size:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f) if any((v or "").strip() for v in r.values())]


def write_csv(path: Path, rows: list[dict[str, Any]], fallback_fields: list[str] | None = None) -> None:
    fields = list(dict.fromkeys((fallback_fields or []) + [k for r in rows for k in r.keys()]))
    if not fields:
        fields = RAW_FIELDS.copy()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: json.dumps(row.get(k), ensure_ascii=False) if isinstance(row.get(k), (dict, list)) else row.get(k, "") for k in fields})
    os.replace(tmp, path)


def parse_engine_csv(raw: bytes) -> list[dict[str, str]]:
    text = raw.decode("utf-8-sig", "replace")
    return [dict(r) for r in csv.DictReader(io.StringIO(text)) if any((v or "").strip() for v in r.values())]


def run_engine_job(cell: dict[str, Any], queries: list[str], grid_size: int, tile_index: int) -> list[dict[str, str]]:
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    # Verify the API before creating a job so a wrong host fails early.
    http_request(f"{API_BASE}/api/v1/jobs", timeout=20, attempts=1)
    body = {
        "name": f"ibb-deep-g{grid_size}-t{tile_index:04d}",
        "keywords": queries,
        "lang": "en",
        "zoom": cell["zoom"],
        "lat": cell["lat"],
        "lon": cell["lon"],
        "fast_mode": False,
        "radius": cell["radius"],
        "depth": 5,
        "email": True,
        "max_time": 600,
    }
    raw = http_request(f"{API_BASE}/api/v1/jobs", data=json.dumps(body).encode("utf-8"),
                       headers=headers, timeout=45, attempts=3)
    job_id = json.loads(raw.decode("utf-8")).get("id")
    if not job_id:
        raise RuntimeError("Engine did not return a job ID")
    print(f"Created {job_id}; grid={grid_size} tile={tile_index} at {cell['lat']},{cell['lon']}", flush=True)
    for poll in range(120):
        payload = json.loads(http_request(f"{API_BASE}/api/v1/jobs/{job_id}", timeout=30).decode("utf-8"))
        status = str(payload.get("Status") or payload.get("status") or "").lower()
        if status in ("ok", "success", "completed", "done"):
            break
        if status in ("failed", "error", "cancelled", "canceled"):
            raise RuntimeError(f"Engine job {job_id} ended with status={status}: {payload}")
        time.sleep(8)
    else:
        raise RuntimeError(f"Engine job {job_id} did not finish before polling deadline")
    raw_csv = http_request(f"{API_BASE}/api/v1/jobs/{job_id}/download", timeout=90, attempts=3)
    rows = parse_engine_csv(raw_csv)
    timestamp = utc_now()
    for row in rows:
        row["scan_grid_size"] = str(grid_size)
        row["scan_tile_index"] = str(tile_index)
        row["scan_completed_at_utc"] = timestamp
    return rows


def osm_query(bbox: list[float]) -> str:
    s, n, w, e = bbox
    box = f"({s},{w},{n},{e})"
    selectors = []
    for tag in ("shop", "amenity", "office", "craft", "tourism", "healthcare", "leisure", "industrial", "commercial", "education"):
        selectors.append(f'nwr["name"]["{tag}"]{box};')
    selectors.extend([
        f'nwr["name"]["phone"]{box};',
        f'nwr["name"]["website"]{box};',
        f'nwr["name"]["contact:phone"]{box};',
        f'nwr["name"]["contact:website"]{box};',
    ])
    return "[out:json][timeout:90];(" + "".join(selectors) + ");out center tags;"


def collect_openstreetmap(state: dict[str, Any]) -> int:
    if state.get("overpass_complete"):
        return len(csv_rows(OSM_CSV))
    bbox = state.get("bbox")
    if not bbox:
        return 0
    query = osm_query([float(x) for x in bbox])
    endpoints = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    ]
    last_error = ""
    for endpoint in endpoints:
        try:
            form = urllib.parse.urlencode({"data": query}).encode("utf-8")
            raw = http_request(endpoint, data=form,
                               headers={"Content-Type": "application/x-www-form-urlencoded"},
                               timeout=110, attempts=1)
            obj = json.loads(raw.decode("utf-8"))
            elements = obj.get("elements", [])
            result = []
            for el in elements:
                tags = el.get("tags") or {}
                name = tags.get("name") or tags.get("name:en") or tags.get("name:ar")
                if not name:
                    continue
                loc = el.get("center") or el
                lat, lon = loc.get("lat"), loc.get("lon")
                typ, oid = el.get("type", "node"), el.get("id", "")
                address_parts = [tags.get(k, "") for k in ("addr:street", "addr:housenumber", "addr:place", "addr:suburb", "addr:city")]
                address = ", ".join(dict.fromkeys(p.strip() for p in address_parts if p and p.strip()))
                if not address:
                    address = ", ".join(p for p in (tags.get("addr:place"), tags.get("addr:city"), "Ibb, Yemen") if p)
                image = tags.get("image", "")
                images = [image] if image.startswith(("https://", "http://")) else []
                row = {
                    "title": name,
                    "category": next((tags.get(k) for k in ("shop","amenity","office","craft","tourism","healthcare","leisure","industrial","commercial","education") if tags.get(k)), "OSM place"),
                    "address": address,
                    "complete_address": json.dumps({"street":tags.get("addr:street",""),"housenumber":tags.get("addr:housenumber",""),"suburb":tags.get("addr:suburb",""),"city":tags.get("addr:city","Ibb"),"country":"YE"}, ensure_ascii=False),
                    "phone": tags.get("phone") or tags.get("contact:phone") or "",
                    "emails": tags.get("email") or tags.get("contact:email") or "",
                    "website": tags.get("website") or tags.get("contact:website") or "",
                    "open_hours": tags.get("opening_hours") or "",
                    "instagram": tags.get("contact:instagram") or "",
                    "facebook": tags.get("contact:facebook") or "",
                    "linkedin": tags.get("contact:linkedin") or "",
                    "latitude": str(lat or ""),
                    "longitude": str(lon or ""),
                    "place_id": f"osm:{typ}:{oid}",
                    "cid": "",
                    "data_id": "",
                    "link": f"https://www.openstreetmap.org/{typ}/{oid}",
                    "google_maps_url": "",
                    "images": json.dumps(images, ensure_ascii=False),
                    "thumbnail": image if images else "",
                    "image": image,
                    "source_engine": "openstreetmap",
                    "osm_tags": json.dumps(tags, ensure_ascii=False),
                }
                result.append(row)
            write_csv(OSM_CSV, result, RAW_FIELDS + ["image","osm_tags","source_engine"])
            state["overpass_complete"] = True
            state["overpass_last_error"] = ""
            state["overpass_rows"] = len(result)
            save_state(state)
            print(f"OpenStreetMap supplement: {len(result)} named objects", flush=True)
            return len(result)
        except Exception as exc:
            last_error = f"{endpoint}: {exc}"
            print(f"Overpass endpoint failed; will try fallback: {last_error}", flush=True)
            time.sleep(2)
    state["overpass_last_error"] = last_error
    save_state(state)
    print(f"OpenStreetMap supplement deferred to a later scheduled run: {last_error}", flush=True)
    return 0


def tile_path(grid_size: int, tile_index: int) -> Path:
    return TILES_ROOT / f"grid_{grid_size}" / f"tile_{tile_index:04d}.csv"


def save_tile(grid_size: int, tile_index: int, rows: list[dict[str, str]]) -> None:
    write_csv(tile_path(grid_size, tile_index), rows, RAW_FIELDS)


def aggregate_current_batch(tile_keys: list[str], state: dict[str, Any]) -> int:
    rows: list[dict[str, str]] = []
    for key in tile_keys:
        try:
            grid_part, tile_part = key.split(":", 1)
            rows.extend(csv_rows(tile_path(int(grid_part), int(tile_part))))
        except Exception as exc:
            print(f"Could not read batch tile {key}: {exc}", flush=True)
    write_csv(S1, rows, RAW_FIELDS)
    state["last_batch_rows"] = len(rows)
    return len(rows)


def candidate_tiles(state: dict[str, Any], grid_size: int, batch_size: int) -> list[int]:
    total = grid_size * grid_size
    completed = state.setdefault("completed_tiles", {})
    attempts = state.setdefault("attempts", {})
    stage_prefix = f"{grid_size}:"
    picks: list[int] = []
    # Retry transient failures first, but never repeat one tile more than once in this run.
    for i in range(min(int(state.get("next_tile_index", 0)), total)):
        key = f"{grid_size}:{i}"
        if key not in completed and 0 < int(attempts.get(key, 0)) < MAX_ATTEMPTS:
            picks.append(i)
            if len(picks) >= batch_size:
                return picks
    cursor = int(state.get("next_tile_index", 0))
    while cursor < total and len(picks) < batch_size:
        key = f"{grid_size}:{cursor}"
        if key not in completed and int(attempts.get(key, 0)) < MAX_ATTEMPTS and cursor not in picks:
            picks.append(cursor)
        cursor += 1
    state["next_tile_index"] = max(int(state.get("next_tile_index", 0)), cursor)
    return picks


def read_database_count() -> int:
    try:
        obj = json.loads(DB_JSON.read_text(encoding="utf-8"))
        return len(obj) if isinstance(obj, list) else 0
    except Exception:
        return 0


def project_unique_count() -> int:
    # Use the same cross-engine union-find merger as final output to estimate progress.
    try:
        import merge_results as mr
        previous = mr.read_previous()
        current = []
        current.extend(mr.read_csv(S1, "mahanaicoach"))
        current.extend(mr.read_csv(S2, "gosom"))
        current.extend(mr.read_csv(OSM_CSV, "openstreetmap"))
        for path in TILES_ROOT.glob("grid_*/*.csv"):
            current.extend(mr.read_csv(path, "mahanaicoach"))
        merged, _, _ = mr.merge_records(previous, current)
        return len(merged)
    except Exception as exc:
        print(f"Could not calculate projected unique count: {exc}", flush=True)
        return read_database_count()


def stage_finished(state: dict[str, Any], grid_size: int) -> bool:
    total = grid_size * grid_size
    completed = state.get("completed_tiles", {})
    attempts = state.get("attempts", {})
    for i in range(total):
        key = f"{grid_size}:{i}"
        if key not in completed and int(attempts.get(key, 0)) < MAX_ATTEMPTS:
            return False
    return int(state.get("next_tile_index", 0)) >= total


def advance_stage_if_ready(state: dict[str, Any]) -> None:
    grid_size = state["grid_stages"][int(state["stage_index"])]
    if not stage_finished(state, grid_size):
        state["scan_status"] = f"grid_{grid_size}_in_progress"
        return
    total_unique = project_unique_count()
    state["projected_unique_records"] = total_unique
    state["current_stage_finished_at_utc"] = utc_now()
    failed = [key for key, attempt in state.get("attempts", {}).items()
              if key.startswith(f"{grid_size}:") and int(attempt) >= MAX_ATTEMPTS and key not in state.get("completed_tiles", {})]
    state.setdefault("permanently_failed_tiles", []).extend(x for x in failed if x not in state.get("permanently_failed_tiles", []))
    if total_unique >= int(state.get("target_unique_records", DEFAULT_TARGET)):
        state["target_reached"] = True
        state["scan_complete"] = True
        state["scan_status"] = "target_reached"
        print(f"Target reached after completing grid {grid_size}: {total_unique} unique records", flush=True)
    elif int(state["stage_index"]) + 1 < len(state["grid_stages"]):
        state["stage_index"] = int(state["stage_index"]) + 1
        next_grid = int(state["grid_stages"][state["stage_index"]])
        state["next_tile_index"] = 0
        state["scan_status"] = f"advancing_to_grid_{next_grid}"
        print(f"Grid {grid_size} complete at {total_unique} unique rows; next pass is grid {next_grid}x{next_grid}", flush=True)
    else:
        state["scan_complete"] = True
        state["scan_status"] = "planned_grids_exhausted_target_not_reached"
        print(f"All planned grid passes completed at {total_unique}; target was not reached", flush=True)


def run_batch(batch_size: int, target: int) -> None:
    state = load_state(target)
    state["batch_runs"] = int(state.get("batch_runs", 0)) + 1
    state["last_batch_tiles"] = []
    state["last_batch_successful_tiles"] = 0
    state["last_batch_errors"] = []
    if state.get("scan_complete"):
        print(f"Spatial scan already ended with status={state.get('scan_status')}; web enrichment can continue.", flush=True)
        collect_openstreetmap(state)
        return
    if not state.get("bbox"):
        state["bbox"] = geocode_bbox()
        save_state(state)
    # One optional OSM attempt per batch until a successful response is saved.
    collect_openstreetmap(state)

    try:
        http_request(f"{API_BASE}/api/v1/jobs", timeout=20, attempts=1)
    except Exception as exc:
        state["scan_status"] = "engine_api_unavailable"
        state["last_batch_errors"] = [str(exc)]
        save_state(state)
        raise RuntimeError(f"Mahanaicoach API is not healthy at {API_BASE}: {exc}") from exc

    queries = read_queries()
    grid_size = int(state["grid_stages"][int(state["stage_index"])])
    cells = grid_cells([float(x) for x in state["bbox"]], grid_size)
    picks = candidate_tiles(state, grid_size, batch_size)
    state["last_batch_tiles"] = [f"{grid_size}:{i}" for i in picks]
    state["last_batch_errors"] = []
    state["scan_status"] = f"grid_{grid_size}_in_progress"
    save_state(state)
    print(json.dumps({
        "run_id": os.getenv("GITHUB_RUN_ID", "local"),
        "grid_size": grid_size, "grid_cell_count": len(cells),
        "batch_size": batch_size, "batch_tiles": picks,
        "query_count": len(queries), "target_unique": state["target_unique_records"],
        "bbox": state["bbox"], "completed_tiles": len(state["completed_tiles"]),
        "gosom_complete": state.get("gosom_complete"),
    }, ensure_ascii=False), flush=True)

    this_batch: list[str] = []
    for tile_index in picks:
        key = f"{grid_size}:{tile_index}"
        attempts = int(state["attempts"].get(key, 0)) + 1
        state["attempts"][key] = attempts
        save_state(state)
        try:
            rows = run_engine_job(cells[tile_index], queries, grid_size, tile_index)
            save_tile(grid_size, tile_index, rows)
            state["completed_tiles"][key] = {
                "attempts": attempts, "rows": len(rows), "completed_at_utc": utc_now(),
                "file": str(tile_path(grid_size, tile_index).relative_to(ROOT)),
            }
            state.get("failed_tiles", {}).pop(key, None)
            this_batch.append(key)
            print(f"Completed tile {key}: {len(rows)} source rows", flush=True)
        except Exception as exc:
            state["failed_tiles"][key] = {"attempts": attempts, "last_error": str(exc)[:1200], "at_utc": utc_now()}
            state["last_batch_errors"].append({"tile": key, "attempt": attempts, "error": str(exc)[:1200]})
            if attempts < MAX_ATTEMPTS:
                print(f"Tile {key} failed; queued for retry ({attempts}/{MAX_ATTEMPTS}): {exc}", flush=True)
            else:
                print(f"Tile {key} failed permanently after {attempts} attempts: {exc}", flush=True)
        save_state(state)

    rows_added = aggregate_current_batch(this_batch, state)
    state["last_batch_successful_tiles"] = len(this_batch)
    state["last_batch_rows"] = rows_added
    state["last_batch_finished_at_utc"] = utc_now()
    state["projected_unique_records"] = project_unique_count()
    advance_stage_if_ready(state)
    save_state(state)
    print(json.dumps({
        "batch_runs": state["batch_runs"],
        "last_batch_tiles": state["last_batch_tiles"],
        "last_batch_rows": state["last_batch_rows"],
        "projected_unique_records": state.get("projected_unique_records", read_database_count()),
        "completed_tile_count": len(state["completed_tiles"]),
        "permanently_failed_tile_count": len(state.get("permanently_failed_tiles", [])),
        "scan_status": state["scan_status"],
        "target_reached": state["target_reached"],
        "scan_complete": state["scan_complete"],
        "last_batch_errors": state["last_batch_errors"],
    }, ensure_ascii=False, indent=2), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("batch","needs-gosom","mark-gosom","status","cleanup-batch"), default="batch")
    ap.add_argument("--batch-size", type=int, default=int(os.getenv("BATCH_SIZE", "10")))
    ap.add_argument("--target", type=int, default=DEFAULT_TARGET)
    args = ap.parse_args()
    if not 1 <= args.batch_size <= 12:
        ap.error("--batch-size must be between 1 and 12")
    state = load_state(args.target)
    if args.mode == "needs-gosom":
        print("yes" if not state.get("gosom_complete") else "no")
        return 0 if not state.get("gosom_complete") else 1
    if args.mode == "mark-gosom":
        state["gosom_complete"] = True
        state["gosom_completed_at_utc"] = utc_now()
        save_state(state)
        print("Marked initial Gosom enrichment pass complete")
        return 0
    if args.mode == "status":
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    if args.mode == "cleanup-batch":
        removed = 0
        for key in state.get("last_batch_tiles", []):
            try:
                grid_part, tile_part = key.split(":", 1)
                path = tile_path(int(grid_part), int(tile_part))
                if path.exists():
                    path.unlink()
                    removed += 1
            except Exception as exc:
                print(f"Could not clean tile {key}: {exc}", flush=True)
        # Clean empty tile folders; completed state and merged database remain the durable checkpoint.
        for directory in sorted(TILES_ROOT.glob("grid_*")):
            try: directory.rmdir()
            except OSError: pass
        save_state(state)
        print(f"Cleaned {removed} tile scratch files after merge", flush=True)
        return 0
    run_batch(args.batch_size, args.target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
