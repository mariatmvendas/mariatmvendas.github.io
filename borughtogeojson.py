
import json
import re
import argparse
import urllib.request
from pathlib import Path
from bs4 import BeautifulSoup


# ── Config ─────────────────────────────────────────────────────────────────

BOROUGH_GEOJSON_URL = (
    "https://data.cityofnewyork.us/api/geospatial/7t3b-ywvw"
    "?method=export&type=GeoJSON"
)
GEOJSON_CACHE = "nyc_boroughs.geojson"

OUTLINE_COLOR     = "#1a1814"
OUTLINE_WIDTH     = 1.5
OUTLINE_OPACITY   = 0.55
LABEL_COLOR       = "#1a1814"
LABEL_SIZE        = 13
LABEL_FONT_FAMILY = "DM Mono, monospace"

# Marker inserted into patched files so we never double-patch
PATCH_MARKER = "<!-- borough-overlay-applied -->"


# ── GeoJSON helpers ─────────────────────────────────────────────────────────

def load_borough_geojson(cache_path: str) -> dict:
    p = Path(cache_path)
    if p.exists():
        print(f"  Using cached GeoJSON: {cache_path}")
        return json.loads(p.read_text(encoding="utf-8"))

    print("  Downloading borough boundaries from NYC Open Data...")
    try:
        with urllib.request.urlopen(BOROUGH_GEOJSON_URL, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        p.write_text(json.dumps(data), encoding="utf-8")
        print(f"  Saved to {cache_path}")
        return data
    except Exception as e:
        print(f"  ✗ Download failed: {e}")
        print(f"  → Save manually as {cache_path}")
        raise SystemExit(1)


def polygon_to_latlon(ring: list) -> tuple:
    lons = [pt[0] for pt in ring] + [ring[0][0], None]
    lats = [pt[1] for pt in ring] + [ring[0][1], None]
    return lons, lats


def extract_borough_outlines(geojson: dict) -> dict:
    outlines = {}
    for feature in geojson.get("features", []):
        props  = feature.get("properties", {})
        name   = props.get("boro_name") or props.get("name") or props.get("NAME", "Unknown")
        gtype  = feature["geometry"]["type"]
        coords = feature["geometry"]["coordinates"]

        all_lons, all_lats = [], []

        if gtype == "Polygon":
            for ring in coords:
                lo, la = polygon_to_latlon(ring)
                all_lons += lo
                all_lats += la
        elif gtype == "MultiPolygon":
            for polygon in coords:
                for ring in polygon:
                    lo, la = polygon_to_latlon(ring)
                    all_lons += lo
                    all_lats += la

        outlines[name] = (all_lons, all_lats)
    return outlines


def extract_borough_centroids(geojson: dict) -> dict:
    # Hardcoded for reliability — avoids centroid landing on water/islands
    hardcoded = {
        "Manhattan":    (-73.971, 40.779),
        "Brooklyn":     (-73.944, 40.650),
        "Queens":       (-73.832, 40.700),
        "Bronx":        (-73.865, 40.837),
        "Staten Island":(-74.151, 40.579),
    }
    centroids = {}
    for feature in geojson.get("features", []):
        props = feature.get("properties", {})
        name  = props.get("boro_name") or props.get("name") or props.get("NAME", "Unknown")
        if name in hardcoded:
            centroids[name] = hardcoded[name]
    return centroids


# ── Trace builders ──────────────────────────────────────────────────────────

def build_outline_trace(outlines: dict) -> dict:
    all_lons, all_lats = [], []
    for lons, lats in outlines.values():
        all_lons += lons
        all_lats += lats

    return {
        "type":       "scattermapbox",
        "mode":       "lines",
        "lon":        all_lons,
        "lat":        all_lats,
        "line":       {"color": OUTLINE_COLOR, "width": OUTLINE_WIDTH},
        "opacity":    OUTLINE_OPACITY,
        "hoverinfo":  "skip",
        "showlegend": False,
        "name":       "",
    }


def build_label_trace(centroids: dict) -> dict:
    """Invisible marker trace — just to anchor the text in mapbox."""
    names = list(centroids.keys())
    lons  = [centroids[n][0] for n in names]
    lats  = [centroids[n][1] for n in names]

    return {
        "type":   "scattermapbox",
        "mode":   "text",
        "lon":    lons,
        "lat":    lats,
        "text":   [n.upper() for n in names],
        "textfont": {
            "size":   14,
            "color":  "#0F0101",
            "family": "Arial Black",
        },
        "textposition": "middle center",
        "hoverinfo":    "skip",
        "showlegend":   False,
        "name":         "",
    }


# ── HTML patcher ────────────────────────────────────────────────────────────

def find_plotly_call(src: str):
    """
    Robustly locate the Plotly.react / newPlot call by finding the
    opening paren and then walking the string to match brackets,
    rather than relying on a regex to span megabytes of JSON.
    """
    m = re.search(r'Plotly\.(react|newPlot)\s*\(', src)
    if not m:
        return None

    start = m.end() - 1   # position of the opening '('
    depth = 0
    i     = start

    while i < len(src):
        c = src[i]
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return src[start + 1 : i]   # content inside the outer parens
        elif c == '"':
            # skip string literals so brackets inside strings don't confuse us
            i += 1
            while i < len(src):
                if src[i] == '\\':
                    i += 2
                    continue
                if src[i] == '"':
                    break
                i += 1
        i += 1

    return None


def split_plotly_args(inner: str):
    """
    Split the content inside Plotly.react(...) into its four arguments:
    div_id, traces_json, layout_json, config_json.
    Returns (div_id_str, traces_str, layout_str, config_str) or None.
    """
    # Find the div id (first string argument)
    m = re.match(r'\s*(?:"[^"]*"|\'[^\']*\')\s*,\s*', inner)
    if not m:
        return None
    div_id  = m.group(0)
    rest    = inner[m.end():]

    # Walk rest to extract exactly 3 top-level JSON values (traces, layout, config)
    parts = []
    i = 0
    while i < len(rest) and len(parts) < 3:
        # skip whitespace and commas between args
        while i < len(rest) and rest[i] in ' \t\n\r,':
            i += 1
        if i >= len(rest):
            break

        start = i
        opener = rest[i]
        if opener not in ('{', '['):
            break

        closer = '}' if opener == '{' else ']'
        depth  = 0

        while i < len(rest):
            c = rest[i]
            if c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    parts.append(rest[start : i + 1])
                    i += 1
                    break
            elif c == '"':
                i += 1
                while i < len(rest):
                    if rest[i] == '\\':
                        i += 2
                        continue
                    if rest[i] == '"':
                        break
                    i += 1
            i += 1

    if len(parts) < 3:
        return None

    return div_id, parts[0], parts[1], parts[2]


def patch_html(html: str, extra_traces: list) -> str | None:
    # Skip already-patched files
    if PATCH_MARKER in html:
        return "ALREADY_PATCHED"

    soup    = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script")

    for script in scripts:
        src = script.string
        if not src or "Plotly." not in src:
            continue

        # Find position of the Plotly call
        call_match = re.search(r'Plotly\.(react|newPlot)\s*\(', src)
        if not call_match:
            continue

        inner = find_plotly_call(src)
        if not inner:
            continue

        result = split_plotly_args(inner)
        if not result:
            continue

        div_id, traces_js, layout_js, config_js = result

        try:
            traces = json.loads(traces_js)
            layout = json.loads(layout_js)
        except json.JSONDecodeError as e:
            print(f"  ✗ JSON parse error: {e}")
            continue

        traces += extra_traces

        fn_name  = call_match.group(0)          # e.g. "Plotly.react("
        new_call = (
            fn_name
            + div_id
            + json.dumps(traces,  separators=(",", ":"))
            + ","
            + json.dumps(layout,  separators=(",", ":"))
            + ","
            + config_js
            + ")"
        )

        call_start = call_match.start()
        # Find the end of the original call in src
        paren_inner = find_plotly_call(src)
        # Locate where the original call ends
        call_end_search = re.search(
            r'Plotly\.(react|newPlot)\s*\(', src
        )
        # Walk to find the closing paren
        depth = 0
        i     = call_end_search.end() - 1
        in_str = False
        while i < len(src):
            c = src[i]
            if c == '"' and not in_str:
                in_str = True
            elif c == '"' and in_str:
                in_str = False
            elif not in_str:
                if c == '(':
                    depth += 1
                elif c == ')':
                    depth -= 1
                    if depth == 0:
                        call_end = i + 1
                        break
            i += 1

        script.string = src[:call_start] + new_call + src[call_end:]

        # Add patch marker to the top of the file
        marker_tag = soup.new_tag("meta")
        marker_tag["name"]    = "borough-overlay"
        marker_tag["content"] = "applied"
        if soup.head:
            soup.head.insert(0, marker_tag)

        patched_html = str(soup)
        # Also embed the text marker so we can detect it quickly
        patched_html = patched_html.replace("</head>", f"{PATCH_MARKER}\n</head>", 1)
        return patched_html

    return None


# ── File processing ─────────────────────────────────────────────────────────

def process_file(path: Path, extra_traces: list) -> bool:
    print(f"\n  Processing: {path.name}")
    html   = path.read_text(encoding="utf-8")
    result = patch_html(html, extra_traces)

    if result == "ALREADY_PATCHED":
        print(f"  ↩ Already patched — skipped.")
        return True
    if result is None:
        print(f"  ✗ Plotly call not found — skipped.")
        return False

    path.write_text(result, encoding="utf-8")
    print(f"  ✓ Done.")
    return True


# ── Entry point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Add borough outlines and labels to NYC Plotly choropleth HTML files."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input",  help="Single input HTML file (edited in place, or use --output)")
    group.add_argument("--folder", help="Process ALL .html files in a folder (recursive)")
    parser.add_argument("--output",  help="Output path (only with --input)")
    parser.add_argument("--geojson", default=GEOJSON_CACHE,
                        help=f"Borough GeoJSON cache path (default: {GEOJSON_CACHE})")
    parser.add_argument("--force",   action="store_true",
                        help="Re-patch files even if already patched")
    args = parser.parse_args()

    print("\nLoading borough boundaries...")
    geojson   = load_borough_geojson(args.geojson)
    outlines  = extract_borough_outlines(geojson)
    centroids = extract_borough_centroids(geojson)
    print(f"  Boroughs found: {', '.join(outlines.keys())}")

    extra_traces = [
        build_outline_trace(outlines),
        build_label_trace(centroids),
    ]

    if args.input:
        src  = Path(args.input)
        dest = Path(args.output) if args.output else src
        html = src.read_text(encoding="utf-8")

        if args.force and PATCH_MARKER in html:
            print("  --force: removing existing patch marker...")
            html = html.replace(PATCH_MARKER, "")

        result = patch_html(html, extra_traces)
        if result == "ALREADY_PATCHED":
            print(f"\n↩ {src.name} already patched. Use --force to re-apply.")
        elif result:
            dest.write_text(result, encoding="utf-8")
            print(f"\n✓ Written to: {dest}")
        else:
            print(f"\n✗ Could not patch {src.name}")

    elif args.folder:
        folder = Path(args.folder)
        files  = sorted(folder.rglob("*.html"))
        if not files:
            print(f"No HTML files found in {folder}")
            return
        print(f"\nFound {len(files)} HTML file(s) in {folder}")

        if args.force:
            print("  --force: stripping existing patch markers...")
            for f in files:
                txt = f.read_text(encoding="utf-8")
                if PATCH_MARKER in txt:
                    f.write_text(txt.replace(PATCH_MARKER, ""), encoding="utf-8")

        ok = sum(process_file(f, extra_traces) for f in files)
        print(f"\nDone — {ok}/{len(files)} files processed.")


if __name__ == "__main__":
    main()