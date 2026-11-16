#!/usr/bin/env python3
"""Build index.html: a self-contained dashboard of Berlin bike thefts.

Inputs  : Fahrraddiebstahl.csv (Polizei Berlin), LOR Excel key list, data_raw/plr_2021.geojson (WFS, LOR 2021)
Output  : index.html (data inlined; no external requests)
Run     : python3 build.py            (rebuild from the local CSV)
          python3 build.py --fetch    (download the latest CSV from Polizei Berlin first, then rebuild)
"""
import argparse, json, math, sys, urllib.request
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
CSV = ROOT / "Fahrraddiebstahl.csv"
GEO = ROOT / "data_raw" / "plr_2021.geojson"
TEMPLATE = ROOT / "template.html"
OUT = ROOT / "index.html"

# Official resource listed on https://daten.berlin.de/datensaetze/fahrraddiebstahl-in-berlin (Polizei Berlin)
CSV_URL = "https://www.polizei-berlin.eu/Fahrraddiebstahl/Fahrraddiebstahl.csv"
EXPECTED_COLS = ["ANGELEGT_AM", "TATZEIT_ANFANG_DATUM", "TATZEIT_ANFANG_STUNDE", "TATZEIT_ENDE_DATUM",
                 "TATZEIT_ENDE_STUNDE", "LOR", "SCHADENSHOEHE", "VERSUCH", "ART_DES_FAHRRADS", "DELIKT", "ERFASSUNGSGRUND"]
RDP_TOL = 0.00007  # degrees, ~8 m
SVG_W = 1000


# ---------- data refresh ----------
def decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


def fetch_csv(url: str = CSV_URL) -> None:
    """Download the CSV, validate it really is the expected file, then replace the local copy."""
    req = urllib.request.Request(url, headers={"User-Agent": "stolen-bikes-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    text = decode(raw)
    head = text.lstrip()[:200].lower()
    if head.startswith("<!doctype") or head.startswith("<html"):
        sys.exit(f"Refusing to use the download: got a web page, not CSV ({len(raw)} bytes) from {url}")
    lines = text.splitlines()
    cols = [c.strip() for c in lines[0].split(",")]
    if cols != EXPECTED_COLS:
        sys.exit(f"Refusing to use the download: unexpected columns {cols}")
    if len(lines) < 1000:
        sys.exit(f"Refusing to use the download: only {len(lines)-1} rows")
    if CSV.exists():
        CSV.replace(CSV.with_name("Fahrraddiebstahl.prev.csv"))
    CSV.write_bytes(raw)
    print(f"downloaded {len(lines)-1} rows ({len(raw)/1e6:.1f} MB) -> {CSV.name} (previous copy kept as Fahrraddiebstahl.prev.csv)")


ap = argparse.ArgumentParser()
ap.add_argument("--fetch", action="store_true", help="download the latest CSV from Polizei Berlin before building")
ap.add_argument("--url", default=CSV_URL, help="CSV address to use with --fetch (default: the Polizei Berlin resource)")
args = ap.parse_args()
if args.fetch:
    fetch_csv(args.url)


# ---------- geometry ----------
def rdp(pts: np.ndarray, tol: float) -> np.ndarray:
    n = len(pts)
    keep = np.zeros(n, bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = pts[i], pts[j]
        seg = pts[i + 1 : j] - a
        d = b - a
        L = math.hypot(*d)
        if L == 0:
            dist = np.hypot(seg[:, 0], seg[:, 1])
        else:
            dist = np.abs(d[0] * seg[:, 1] - d[1] * seg[:, 0]) / L
        k = int(dist.argmax())
        if dist[k] > tol:
            m = i + 1 + k
            keep[m] = True
            stack += [(i, m), (m, j)]
    return pts[keep]


def polys(geom):
    return geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]


geo = json.loads(GEO.read_text())
allpts = np.concatenate([np.array(r) for f in geo["features"] for p in polys(f["geometry"]) for r in p])
lon0, lon1 = allpts[:, 0].min(), allpts[:, 0].max()
lat0, lat1 = allpts[:, 1].min(), allpts[:, 1].max()
kx = math.cos(math.radians((lat0 + lat1) / 2))
scale = SVG_W / ((lon1 - lon0) * kx)
SVG_H = round((lat1 - lat0) * scale)


def to_path(geom) -> str:
    parts = []
    for poly in polys(geom):
        for ring in poly:
            r = rdp(np.array(ring, float), RDP_TOL)
            if len(r) < 4:
                continue
            xs = (r[:, 0] - lon0) * kx * scale
            ys = (lat1 - r[:, 1]) * scale
            pts = [f"{x:.1f} {y:.1f}" for x, y in zip(xs, ys)]
            parts.append("M" + "L".join(pts[:-1]) + "Z")
    return "".join(parts)


# ---------- lookup tables ----------
# The LOR 2021 boundary file carries the keys and names of all levels, e.g. bez = "01 - Mitte".
plr_meta, bez_names = {}, {}
for f in geo["features"]:
    pr = f["properties"]
    bez_id, bez_name = (x.strip() for x in pr["bez"].split(" - ", 1))
    plr_meta[pr["plr_id"]] = (pr["plr_name"], pr["bzr_name"], bez_id)
    bez_names[bez_id] = bez_name
assert len(plr_meta) == len(geo["features"]) == 542 and len(bez_names) == 12, "unexpected LOR structure in the boundary file"

# ---------- population (Amt für Statistik, Einwohnerregister, 31.12.2024, LOR 2021) ----------
# data_raw/ewr/pop_2024.csv = RAUMID + E_E (residents) per planning area. The source file lists the
# uninhabited area 06200418 under the ID 06200318 (BZR digit typo); it has 0 residents either way.
POP_ID_ALIAS = {"06200318": "06200418"}
pop_df = pd.read_csv(ROOT / "data_raw" / "ewr" / "pop_2024.csv", dtype={"RAUMID": str})
pop_df["RAUMID"] = pop_df.RAUMID.replace(POP_ID_ALIAS)
assert pop_df.RAUMID.is_unique and set(pop_df.RAUMID) == set(plr_meta), "population IDs do not match the LOR 2021 boundaries"
pop = dict(zip(pop_df.RAUMID, pop_df["pop"].astype(int)))

areas = []
for f in sorted(geo["features"], key=lambda f: f["properties"]["plr_id"]):
    pid = f["properties"]["plr_id"]
    name, bzr, bez_id = plr_meta[pid]
    areas.append(
        dict(
            id=pid,
            name=name,
            bzr=bzr,
            bez=bez_id,
            pop=pop[pid],
            km2=round(f["properties"]["finhalt"] / 1e6, 4),
            d=to_path(f["geometry"]),
        )
    )
plr_idx = {a["id"]: i for i, a in enumerate(areas)}
bezirke = [dict(id=k, name=v) for k, v in sorted(bez_names.items())]

# ---------- cases ----------
import io
df = pd.read_csv(io.StringIO(decode(CSV.read_bytes())), dtype={"LOR": str, "TATZEIT_ANFANG_STUNDE": str})
assert list(df.columns) == EXPECTED_COLS, f"unexpected columns: {list(df.columns)}"
unknown = ~df.LOR.isin(plr_idx)
if unknown.any():  # e.g. a future LOR version; cannot be placed on the map
    print(f"WARNING: dropping {int(unknown.sum())} rows with unknown LOR ids: {sorted(df.LOR[unknown].unique())[:10]}")
df = df[~unknown].copy()
start = pd.to_datetime(df.TATZEIT_ANFANG_DATUM + " " + df.TATZEIT_ANFANG_STUNDE + ":00", format="%d.%m.%Y %H:%M")
end = pd.to_datetime(df.TATZEIT_ENDE_DATUM + " " + df.TATZEIT_ENDE_STUNDE.astype(str) + ":00", format="%d.%m.%Y %H:%M")
dur = (end - start).dt.total_seconds() / 3600

types = df.ART_DES_FAHRRADS.value_counts().index.tolist()
delikte = sorted(df.DELIKT.unique())

first = start.min()
DAY0 = date(first.year, first.month, 1)  # day 0 = first day of the earliest month in the data
day = (start.dt.normalize() - pd.Timestamp(DAY0)).dt.days
month = (start.dt.year - DAY0.year) * 12 + start.dt.month - DAY0.month
hour = np.where(dur <= 3, start.dt.hour, -1)

cases = dict(
    p=df.LOR.map(plr_idx).astype(int).tolist(),
    d=day.astype(int).tolist(),
    m=month.astype(int).tolist(),
    h=[int(x) for x in hour],
    v=df.SCHADENSHOEHE.astype(int).tolist(),
    t=df.ART_DES_FAHRRADS.map({t: i for i, t in enumerate(types)}).astype(int).tolist(),
    k=df.DELIKT.map({t: i for i, t in enumerate(delikte)}).astype(int).tolist(),
    a=(df.VERSUCH == "Ja").astype(int).tolist(),
)
assert min(cases["d"]) >= 0 and min(cases["m"]) >= 0

n_months = max(cases["m"]) + 1
def month_start(m):
    y, mo = divmod(DAY0.month - 1 + m, 12)
    return (date(DAY0.year + y, mo + 1, 1) - DAY0).days
month_starts = [month_start(m) for m in range(n_months + 1)]
min_day, max_day = int(min(cases["d"])), int(max(cases["d"]))
years = []
for y in range(DAY0.year, (DAY0 + timedelta(days=max_day)).year + 1):
    a = max(min_day, (date(y, 1, 1) - DAY0).days)
    b = min(max_day, (date(y, 12, 31) - DAY0).days)
    years.append([y, a, b])
updated = pd.to_datetime(df.ANGELEGT_AM.str[:10], format="%d.%m.%Y").max().date().isoformat()

meta = dict(
    day0=DAY0.isoformat(),
    minDay=min_day,
    maxDay=max_day,
    years=years,
    updated=updated,
    monthStarts=month_starts,
    svgW=SVG_W,
    svgH=SVG_H,
    types=types,
    delikte=delikte,
    bezirke=bezirke,
    n=len(df),
)

payload = json.dumps(dict(meta=meta, areas=areas, cases=cases), ensure_ascii=False, separators=(",", ":"))
OUT.write_text(TEMPLATE.read_text().replace("/*__DATA__*/null", payload))
print(f"wrote {OUT} ({OUT.stat().st_size/1e6:.2f} MB), {len(areas)} areas, {len(df)} cases, svg {SVG_W}x{SVG_H}")
