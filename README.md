# OSM Downloader

[![Run on Replit](https://replit.com/badge/github/humbug1251/osm_downloader)](https://replit.com/github/humbug1251/osm_downloader)

Streamlit app to download OpenStreetMap features for an extent defined by a shapefile, KML/KMZ, or place name. Data source: Overpass API. Geocoding: Nominatim.

Features:

- Extent from place name, shapefile upload (.zip or components), or KML/KMZ upload
- Feature presets (roads, buildings, water, landuse, ...) plus free-form OSM tags (`key=value`, `key~regex`, `key!=value`)
- Optional buffer in meters around the extent
- Progress bar: download size/percent, element conversion, clipping
- Map preview, then download as GPKG, GeoJSON, or zipped Shapefile

## Run locally

```
pip install -r requirements.txt
streamlit run app.py
```

Opens at http://localhost:8501

## Deploy on Streamlit Community Cloud (free public URL)

1. Go to https://share.streamlit.io and sign in with GitHub.
2. Click "Create app" -> "Deploy a public app from GitHub".
3. Select repository `humbug1251/osm_downloader`, branch `main`, main file path `app.py`.
4. Pick an app URL, e.g. `https://osm-downloader.streamlit.app`, and click Deploy.

Every `git push` to `main` redeploys automatically. If the repo is not listed, grant the Streamlit OAuth app access to it on GitHub.

## Deploy on Replit

1. Open https://replit.com/github.com/humbug1251/osm_downloader (or use replit.com/import).
2. Replit installs dependencies from `requirements.txt`; press Run to start the app.
3. Click Publish for a permanent URL (Replit requires a payment method for published deployments; on the free plan the app runs while the Repl is open).

The committed `.replit` file already configures the Streamlit run/publish command and port.

## CLI usage

`download_osm.py` also works as a standalone command:

```
python download_osm.py --shapefile extent.shp --features roads,buildings --out osm.gpkg
python download_osm.py --kml Site.kml --buffer 500
python download_osm.py --area "Shiggaon, Karnataka, India" --features none --tags "amenity=school,natural=tree"
```

Notes for hosted deployments: the host needs outbound internet to reach overpass-api.de and nominatim.openstreetmap.org; free Streamlit apps sleep after inactivity and wake on the next visit.
