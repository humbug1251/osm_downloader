"""Streamlit webapp for downloading OSM features.

Run with:
  streamlit run osm_webapp.py
"""

import argparse
import io
import tempfile
import zipfile
from pathlib import Path

import folium
import geopandas as gpd
import streamlit as st
from streamlit_folium import st_folium

import download_osm as osm

GEOM_COLORS = {
    "Point": "#e41a1c",
    "MultiPoint": "#e41a1c",
    "LineString": "#377eb8",
    "MultiLineString": "#377eb8",
    "Polygon": "#4daf4a",
    "MultiPolygon": "#4daf4a",
}

st.set_page_config(page_title="OSM Downloader", layout="wide")
st.title("OSM Data Downloader")
st.caption("Define the extent with a shapefile, KML/KMZ, or place name; data comes from the Overpass API.")


def _store_shapefile(files, workdir):
    if not files:
        raise ValueError("Upload a .zip of the shapefile, or all its component files (.shp, .dbf, .shx, .prj)")
    if len(files) == 1 and files[0].name.lower().endswith(".zip"):
        path = workdir / files[0].name
        path.write_bytes(files[0].getvalue())
        return path
    for f in files:
        (workdir / f.name).write_bytes(f.getvalue())
    shp = next(workdir.glob("*.shp"), None)
    if shp is None:
        raise ValueError("No .shp file among the uploaded files")
    return shp


def _store_kml(kml_file, workdir):
    if kml_file is None:
        raise ValueError("Upload a .kml or .kmz file")
    path = workdir / kml_file.name
    path.write_bytes(kml_file.getvalue())
    return path


def resolve_extent(mode, area_text, shp_files, kml_file, workdir):
    ns = argparse.Namespace(shapefile=None, kml=None, area=None)
    if mode == "Place name":
        if not area_text.strip():
            raise ValueError("Enter a place name")
        ns.area = area_text.strip()
    elif mode == "Upload shapefile":
        ns.shapefile = str(_store_shapefile(shp_files, workdir))
    else:
        ns.kml = str(_store_kml(kml_file, workdir))
    return osm.resolve_input(ns)


def make_outputs(gdf, fmt, workdir):
    if fmt == "GeoJSON":
        return gdf.to_json().encode("utf-8"), "osm_download.geojson", "application/geo+json"
    if fmt == "GPKG":
        target = workdir / "osm_download.gpkg"
        gdf.to_file(target)
        return target.read_bytes(), target.name, "application/geopackage+sqlite3"
    shp_dir = workdir / "shp"
    shp_dir.mkdir(exist_ok=True)
    osm.save(gdf, shp_dir / "osm_download.shp")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in shp_dir.iterdir():
            z.write(p, p.name)
    return buf.getvalue(), "osm_download_shp.zip", "application/zip"


def build_map(gdf, clip_geom, bbox):
    south, west, north, east = bbox
    m = folium.Map(location=[(south + north) / 2, (west + east) / 2], tiles="OpenStreetMap")
    if clip_geom is not None:
        folium.GeoJson(
            clip_geom.__geo_interface__,
            name="Extent",
            style_function=lambda _: {"color": "#ff7800", "weight": 2, "fill": False},
        ).add_to(m)
    else:
        folium.Rectangle(
            bounds=[(south, west), (north, east)],
            color="#ff7800",
            weight=2,
            fill=False,
            tooltip="Extent",
        ).add_to(m)
    for geom_type, sub in gdf.groupby(gdf.geom_type):
        color = GEOM_COLORS.get(geom_type, "#984ea3")
        group = folium.FeatureGroup(name=f"{geom_type} ({len(sub)})")
        folium.GeoJson(
            sub.to_json(),
            style_function=lambda _, c=color: {"color": c, "weight": 2, "fillOpacity": 0.35},
            marker=folium.CircleMarker(radius=5, color=color, fill=True, fill_opacity=0.8),
        ).add_to(group)
        group.add_to(m)
    folium.LayerControl().add_to(m)
    m.fit_bounds([[south, west], [north, east]])
    return m


with st.sidebar:
    st.header("Input")
    mode = st.radio("Extent from", ["Place name", "Upload shapefile", "Upload KML / KMZ"])
    area_text = None
    shp_files = None
    kml_file = None
    if mode == "Place name":
        area_text = st.text_input("Place name", "Shiggaon, Karnataka, India")
    elif mode == "Upload shapefile":
        shp_files = st.file_uploader(
            "Shapefile (.zip, or .shp/.dbf/.shx/.prj together)",
            type=["zip", "shp", "dbf", "shx", "prj", "cpg"],
            accept_multiple_files=True,
        )
    else:
        kml_file = st.file_uploader("KML / KMZ", type=["kml", "kmz"])

    st.header("Data")
    features = st.multiselect(
        "Features",
        list(osm.FEATURE_FILTERS),
        default=["roads", "buildings", "water"],
    )
    custom_tags = st.text_area(
        "Extra OSM tags (optional, one per line)",
        placeholder="amenity=school\nnatural=tree\nhighway~bus_stop\nbuilding!=yes",
        height=110,
    )
    st.caption("Any OSM tag: `key`, `key=value`, `key~regex`, `key!=value`")
    buffer_m = st.number_input("Buffer around extent (m)", min_value=0.0, max_value=50000.0, value=0.0, step=50.0)
    fmt = st.radio("Output format", ["GPKG", "GeoJSON", "Shapefile (zip)"])
    run = st.button("Download OSM data", type="primary", use_container_width=True)

if run:
    if not features and not custom_tags.strip():
        st.error("Select at least one feature type or enter custom OSM tags")
    else:
        bar = st.progress(0.0, text="Resolving extent...")
        try:
            with tempfile.TemporaryDirectory(prefix="osm_web_") as tmp:
                workdir = Path(tmp)
                bbox, clip_geom, label = resolve_extent(mode, area_text or "", shp_files, kml_file, workdir)
                if buffer_m > 0:
                    bbox = osm.expand_bbox(bbox, buffer_m)
                    if clip_geom is not None:
                        clip_geom = osm.buffer_geometry(clip_geom, buffer_m)
                bar.progress(0.08, text=f"Extent: {label}")

                def download_progress(received, total):
                    if total:
                        frac = 0.10 + 0.65 * min(received / total, 0.95)
                        bar.progress(frac, text=f"Downloading from Overpass... {received / 1e6:.2f} of {total / 1e6:.2f} MB")
                    else:
                        bar.progress(0.45, text=f"Downloading from Overpass... {received / 1e6:.2f} MB")

                query = osm.build_query(bbox, features, 180, custom_tags)
                data = osm.overpass_query(query, 180, progress=download_progress)

                n_elements = max(len(data.get("elements", [])), 1)

                def parse_progress(i, total):
                    bar.progress(0.75 + 0.20 * i / n_elements, text=f"Converting elements... {i} of {total}")

                gdf = osm.elements_to_gdf(data, progress=parse_progress)
                bar.progress(0.96, text="Clipping and writing output...")
                if clip_geom is not None and not gdf.empty:
                    mask = gpd.GeoDataFrame(geometry=[clip_geom], crs="EPSG:4326")
                    gdf = gpd.clip(gdf, mask)
                    gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()]
                if gdf.empty:
                    file_bytes, file_name, mime = None, None, None
                else:
                    file_bytes, file_name, mime = make_outputs(gdf, fmt, workdir)
                st.session_state["result"] = {
                    "gdf": gdf,
                    "clip_geom": clip_geom,
                    "bbox": bbox,
                    "label": label,
                    "raw_count": len(data.get("elements", [])),
                    "file_bytes": file_bytes,
                    "file_name": file_name,
                    "mime": mime,
                    "tags": custom_tags.strip(),
                }
                bar.progress(1.0, text="Done")
        except Exception as exc:
            bar.empty()
            st.session_state.pop("result", None)
            st.error(f"Download failed: {exc}")

result = st.session_state.get("result")
if result is None:
    st.info("Choose an extent on the left and click Download OSM data.")
else:
    gdf = result["gdf"]
    st.subheader("Result")
    st.write(f"Extent: {result['label']}")
    if result.get("tags"):
        st.write(f"Custom tags: {result['tags']}")
    if gdf.empty:
        st.warning("No features found for the given extent and feature selection.")
    else:
        col1, col2, col3 = st.columns(3)
        col1.metric("Features", len(gdf))
        col2.metric("Elements from Overpass", result["raw_count"])
        col3.metric("Geometry types", gdf.geom_type.nunique())
        st.dataframe(gdf.groupby(gdf.geom_type).size().rename("count").rename_axis("geometry"))
        st_folium(build_map(gdf, result["clip_geom"], result["bbox"]), height=560, use_container_width=True, returned_objects=[])
        st.download_button(
            f"Download {result['file_name']}",
            data=result["file_bytes"],
            file_name=result["file_name"],
            mime=result["mime"],
            type="primary",
        )
