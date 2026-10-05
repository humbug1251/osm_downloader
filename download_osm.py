"""Download OpenStreetMap features for an extent given by a shapefile, KML/KMZ, or place name.

Usage examples:
  python download_osm.py --shapefile extent.shp --features roads,buildings --out osm.gpkg
  python download_osm.py --kml Site.kml --buffer 500
  python download_osm.py --area "Shiggaon, Karnataka, India" --features roads,water --out shiggaon.gpkg
  python download_osm.py --area "Shiggaon, Karnataka, India" --features "" --tags "amenity=school,natural=tree"

Features: roads, rail, buildings, water, landuse, amenities, power, boundaries, all
Custom tags (--tags): any OSM tag, e.g. "amenity=school", "highway=bus_stop", "name~Temple", "building!=yes"
Output: .gpkg or .geojson (recommended). For .shp a file per geometry type is written.
"""

import argparse
import json
import sys
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import geopandas as gpd
import requests
from pyproj import CRS, Transformer
from shapely.geometry import LineString, MultiPoint, Point, Polygon, box, shape
from shapely.ops import transform as shapely_transform
from shapely.ops import unary_union

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
HEADERS = {"User-Agent": "mvs-osm-downloader/1.0 (local GIS script)"}

FEATURE_FILTERS = {
    "roads": ['way["highway"]'],
    "rail": ['way["railway"]'],
    "buildings": ['way["building"]', 'relation["building"]'],
    "water": [
        'way["natural"="water"]',
        'way["waterway"]',
        'way["water"]',
        'relation["natural"="water"]',
        'relation["waterway"]',
    ],
    "landuse": ['way["landuse"]', 'relation["landuse"]'],
    "amenities": ['node["amenity"]', 'node["shop"]', 'node["healthcare"]', 'node["office"]'],
    "power": ['way["power"]', 'node["power"]'],
    "boundaries": ['relation["boundary"="administrative"]'],
}


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _kml_root(path):
    path = Path(path)
    if path.suffix.lower() == ".kmz":
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".kml"))
            return ET.fromstring(z.read(name))
    return ET.parse(path).getroot()


def _kml_coords(path):
    root = _kml_root(path)
    coords = []
    for elem in root.iter():
        name = _local(elem.tag)
        if name == "coordinates" and elem.text:
            for token in elem.text.split():
                parts = token.split(",")
                if len(parts) >= 2:
                    coords.append((float(parts[0]), float(parts[1])))
        elif name == "coord" and elem.text:
            parts = elem.text.split()
            if len(parts) >= 2:
                coords.append((float(parts[0]), float(parts[1])))
    return coords


def _ring_coords(boundary):
    for elem in boundary.iter():
        if _local(elem.tag) != "coordinates" or not elem.text:
            continue
        pts = []
        for token in elem.text.split():
            parts = token.split(",")
            if len(parts) >= 2:
                pts.append((float(parts[0]), float(parts[1])))
        if len(pts) >= 4 and pts[0] == pts[-1]:
            return pts
        if len(pts) >= 3:
            return pts + [pts[0]]
    return None


def kml_clip_geometry(path):
    root = _kml_root(path)
    polys = []
    for elem in root.iter():
        if _local(elem.tag) != "Polygon":
            continue
        outer = next((c for c in elem.iter() if _local(c.tag) == "outerBoundaryIs"), None)
        if outer is None:
            continue
        shell = _ring_coords(outer)
        if not shell:
            continue
        holes = []
        for inner in (c for c in elem.iter() if _local(c.tag) == "innerBoundaryIs"):
            ring = _ring_coords(inner)
            if ring and ring not in holes:
                holes.append(ring)
        try:
            polys.append(Polygon(shell, holes))
        except Exception:
            continue
    return unary_union(polys) if polys else None


def bbox_of_geometry(geom):
    minx, miny, maxx, maxy = geom.bounds
    return (miny, minx, maxy, maxx)


def _aeqd_transformers(lat, lon):
    local = CRS.from_proj4(
        f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs"
    )
    return (
        Transformer.from_crs("EPSG:4326", local, always_xy=True),
        Transformer.from_crs(local, "EPSG:4326", always_xy=True),
    )


def expand_bbox(bbox, meters):
    if meters <= 0:
        return bbox
    south, west, north, east = bbox
    to_local, to_wgs = _aeqd_transformers((south + north) / 2, (west + east) / 2)
    x0, y0 = to_local.transform(west, south)
    x1, y1 = to_local.transform(east, north)
    west2, south2 = to_wgs.transform(x0 - meters, y0 - meters)
    east2, north2 = to_wgs.transform(x1 + meters, y1 + meters)
    return (min(south, south2), min(west, west2), max(north, north2), max(east, east2))


def buffer_geometry(geom, meters):
    if meters <= 0:
        return geom
    c = geom.centroid
    to_local, to_wgs = _aeqd_transformers(c.y, c.x)
    projected = shapely_transform(lambda x, y: to_local.transform(x, y), geom)
    return shapely_transform(lambda x, y: to_wgs.transform(x, y), projected.buffer(meters))


def read_vector(path):
    try:
        return gpd.read_file(path)
    except Exception:
        if Path(path).suffix.lower() != ".zip":
            raise
        with tempfile.TemporaryDirectory(prefix="osm_shp_") as tmp:
            with zipfile.ZipFile(path) as z:
                z.extractall(tmp)
            shp = next(Path(tmp).rglob("*.shp"), None)
            if shp is None:
                raise
            return gpd.read_file(shp)


def resolve_input(args):
    if args.shapefile:
        gdf = read_vector(args.shapefile)
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        gdf = gdf.to_crs("EPSG:4326")
        geom = gdf.geometry.union_all()
        clip = geom if geom.geom_type in ("Polygon", "MultiPolygon") else None
        return bbox_of_geometry(geom), clip, Path(args.shapefile).name

    if args.kml:
        clip = kml_clip_geometry(args.kml)
        if clip is not None:
            return bbox_of_geometry(clip), clip, Path(args.kml).name
        pts = _kml_coords(args.kml)
        if not pts:
            sys.exit("No coordinates found in the KML/KMZ file")
        return bbox_of_geometry(MultiPoint(pts)), None, Path(args.kml).name

    resp = requests.get(
        NOMINATIM_URL,
        params={"q": args.area, "format": "jsonv2", "limit": 1, "polygon_geojson": 1},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data:
        sys.exit(f"Nominatim found no place matching: {args.area}")
    info = data[0]
    south, north, west, east = (float(v) for v in info["boundingbox"])
    geom = shape(info["polygon_geojson"]) if info.get("polygon_geojson") else box(west, south, east, north)
    clip = geom if geom.geom_type in ("Polygon", "MultiPolygon") else None
    bbox = bbox_of_geometry(geom) if clip is not None else (south, west, north, east)
    return bbox, clip, info.get("display_name", args.area)


def parse_custom_tags(text):
    filters = []
    for item in text.replace("\n", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if "!=" in item:
            key, value = item.split("!=", 1)
            filters.append(f'nwr["{key.strip()}"!="{value.strip()}"]')
        elif "~" in item:
            key, value = item.split("~", 1)
            filters.append(f'nwr["{key.strip()}"~"{value.strip()}"]')
        elif "=" in item:
            key, value = item.split("=", 1)
            filters.append(f'nwr["{key.strip()}"="{value.strip()}"]')
        else:
            filters.append(f'nwr["{item}"]')
    return filters


def build_query(bbox, features, timeout, custom_tags=""):
    south, west, north, east = bbox
    bbox_str = f"{south:.7f},{west:.7f},{north:.7f},{east:.7f}"
    filters = []
    for name in features:
        for filt in FEATURE_FILTERS[name]:
            filters.append(f"{filt}({bbox_str});")
    for filt in parse_custom_tags(custom_tags):
        filters.append(f"{filt}({bbox_str});")
    if not filters:
        raise ValueError("No OSM features selected: choose presets and/or custom tags")
    return "[out:json][timeout:%d];\n(\n  %s\n);\nout geom;" % (timeout, "\n  ".join(filters))


def overpass_query(query, timeout):
    last_error = None
    for endpoint in OVERPASS_ENDPOINTS:
        for attempt in range(2):
            try:
                resp = requests.post(endpoint, data={"data": query}, headers=HEADERS, timeout=timeout + 30)
                if resp.status_code in (429, 502, 503, 504):
                    last_error = RuntimeError(f"{endpoint}: HTTP {resp.status_code}")
                    time.sleep(10 * (attempt + 1))
                    continue
                resp.raise_for_status()
                data = resp.json()
                if "elements" not in data:
                    raise RuntimeError(f"{endpoint}: unexpected response {str(data)[:200]}")
                return data
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                time.sleep(5)
    raise RuntimeError(f"All Overpass endpoints failed: {last_error}")


def _way_geometry(el):
    coords = [(p["lon"], p["lat"]) for p in el.get("geometry") or [] if p]
    if len(coords) < 2:
        return None
    tags = el.get("tags", {})
    closed = coords[0] == coords[-1]
    area_keys = ("building", "landuse", "leisure", "amenity", "natural", "water", "area", "shop")
    if closed and any(k in tags for k in area_keys):
        try:
            return Polygon(coords)
        except Exception:
            return LineString(coords)
    return LineString(coords)


def _assemble_rings(ways):
    segments = [list(map(tuple, w)) for w in ways if len(w) >= 2]
    rings = []
    while segments:
        ring = segments.pop(0)
        extended = True
        while extended and not (len(ring) > 3 and ring[0] == ring[-1]):
            extended = False
            for i, seg in enumerate(segments):
                if seg[0] == ring[-1]:
                    ring += seg[1:]
                elif seg[-1] == ring[-1]:
                    ring += seg[::-1][1:]
                elif seg[-1] == ring[0]:
                    ring = seg[:-1] + ring
                elif seg[0] == ring[0]:
                    ring = seg[::-1][:-1] + ring
                else:
                    continue
                segments.pop(i)
                extended = True
                break
        if len(ring) > 3 and ring[0] == ring[-1]:
            rings.append(ring)
    return rings


def _relation_geometry(el):
    outers, inners = [], []
    for member in el.get("members", []):
        if member.get("type") != "way" or not member.get("geometry"):
            continue
        coords = [(p["lon"], p["lat"]) for p in member["geometry"] if p]
        if len(coords) < 2:
            continue
        (inners if member.get("role") == "inner" else outers).append(coords)
    outer_rings = _assemble_rings(outers)
    inner_rings = _assemble_rings(inners)
    polys = []
    for ring in outer_rings:
        try:
            poly = Polygon(ring)
        except Exception:
            continue
        holes = []
        for inner in inner_rings:
            try:
                hole = Polygon(inner)
                if poly.contains(hole.representative_point()):
                    holes.append(inner)
            except Exception:
                continue
        if holes:
            try:
                poly = Polygon(ring, holes)
            except Exception:
                pass
        polys.append(poly)
    if not polys:
        return None
    return unary_union(polys)


def elements_to_gdf(data):
    rows = []
    for el in data.get("elements", []):
        el_type = el.get("type")
        if el_type == "node" and "lat" in el and "lon" in el:
            geom = Point(el["lon"], el["lat"])
        elif el_type == "way":
            geom = _way_geometry(el)
        elif el_type == "relation":
            geom = _relation_geometry(el)
        else:
            geom = None
        if geom is None or geom.is_empty:
            continue
        row = {"osm_id": el.get("id"), "osm_type": el_type, "geometry": geom}
        row.update(el.get("tags", {}))
        rows.append(row)
    if not rows:
        return gpd.GeoDataFrame({"osm_id": [], "osm_type": []}, geometry=[], crs="EPSG:4326")
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


def save(gdf, out_path):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() == ".shp":
        for geom_type, sub in gdf.groupby(gdf.geom_type):
            target = out_path.with_name(f"{out_path.stem}_{geom_type}{out_path.suffix}")
            sub.to_file(target)
            print(f"  saved {len(sub):>6} {geom_type:<15} -> {target}")
    else:
        gdf.to_file(out_path)
        print(f"  saved {len(gdf)} features -> {out_path}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download OSM features for a shapefile, KML/KMZ, or place-name extent."
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--shapefile", "-s", help="Shapefile/GeoPackage whose extent is used")
    src.add_argument("--kml", "-k", help="KML or KMZ file whose extent is used")
    src.add_argument("--area", "-a", help="Place name, e.g. \"Shiggaon, Karnataka, India\"")
    parser.add_argument(
        "--features", "-f", default="all",
        help="Comma separated: " + ", ".join(FEATURE_FILTERS) + ", all, none (default: all)",
    )
    parser.add_argument(
        "--tags", "-t", default="",
        help='Extra OSM tags, e.g. "amenity=school,natural=tree" '
             '(supports key, key=value, key~regex, key!=value; combine with --features none)',
    )
    parser.add_argument("--buffer", "-b", type=float, default=0.0,
                        help="Extra buffer around the extent in meters (default: 0)")
    parser.add_argument("--out", "-o", default="osm_download.gpkg",
                        help="Output .gpkg/.geojson (default: osm_download.gpkg)")
    parser.add_argument("--raw", action="store_true", help="Also save the raw Overpass JSON")
    parser.add_argument("--timeout", type=int, default=180, help="Overpass query timeout in seconds")
    return parser.parse_args()


def main():
    args = parse_args()
    features = [f.strip().lower() for f in args.features.split(",") if f.strip()]
    if "none" in features:
        features = []
    if "all" in features:
        features = list(FEATURE_FILTERS)
    unknown = [f for f in features if f not in FEATURE_FILTERS]
    if unknown:
        sys.exit(f"Unknown feature(s): {', '.join(unknown)}. Choose from: {', '.join(FEATURE_FILTERS)}, all")
    if not features and not args.tags.strip():
        sys.exit("Nothing to download: choose --features and/or --tags")

    print(f"Resolving extent from {'shapefile' if args.shapefile else 'KML' if args.kml else 'place name'}...")
    bbox, clip_geom, label = resolve_input(args)
    if args.buffer > 0:
        bbox = expand_bbox(bbox, args.buffer)
        if clip_geom is not None:
            clip_geom = buffer_geometry(clip_geom, args.buffer)
    print(f"  extent: {label}")
    print(f"  bbox (S,W,N,E): {bbox[0]:.6f}, {bbox[1]:.6f}, {bbox[2]:.6f}, {bbox[3]:.6f}")

    query = build_query(bbox, features, args.timeout, args.tags)
    selected = ", ".join(features) if features else ""
    if args.tags.strip():
        selected = f"{selected}, tags: {args.tags.strip()}" if selected else f"tags: {args.tags.strip()}"
    print(f"Downloading from Overpass: {selected} ...")
    data = overpass_query(query, args.timeout)
    print(f"  received {len(data.get('elements', []))} elements")

    if args.raw:
        raw_path = Path(args.out).with_suffix(".osm.json")
        raw_path.write_text(json.dumps(data), encoding="utf-8")
        print(f"  raw JSON -> {raw_path}")

    gdf = elements_to_gdf(data)
    if clip_geom is not None and not gdf.empty:
        mask = gpd.GeoDataFrame(geometry=[clip_geom], crs="EPSG:4326")
        gdf = gpd.clip(gdf, mask)
        gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()]

    if gdf.empty:
        print("No features found for the given extent/features.")
        return

    counts = gdf.groupby(gdf.geom_type).size().to_dict()
    print("Geometry counts: " + ", ".join(f"{k}: {v}" for k, v in counts.items()))
    save(gdf, args.out)


if __name__ == "__main__":
    main()
