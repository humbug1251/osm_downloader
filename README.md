# OSM Downloader

Streamlit app to download OpenStreetMap features for an extent defined by a shapefile, KML/KMZ, or place name.

Data source: Overpass API. Geocoding: Nominatim.

## Run locally

```
pip install -r requirements.txt
streamlit run app.py
```

Opens at http://localhost:8501

## Deploy on Streamlit Community Cloud (free public URL)

1. Install Git and create a GitHub account if you do not have one.
2. Create an empty GitHub repository, e.g. `osm-downloader` (public or private).
3. In this folder run (replace YOUR-USERNAME):

```
git init
git add .
git commit -m "OSM downloader webapp"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/osm-downloader.git
git push -u origin main
```

4. Go to https://share.streamlit.io and sign in with GitHub.
5. Click "Create app" -> "Deploy a public app from GitHub".
6. Select repository `osm-downloader`, branch `main`, main file path `app.py`.
7. Click Deploy. The app gets a URL like `https://osm-downloader.streamlit.app`.

Notes:

- Keep the app Public in Streamlit, or add a viewer allowlist for private access.
- The host needs outbound internet to reach overpass-api.de and nominatim.openstreetmap.org.
- Free tier: apps sleep after inactivity and wake on the next visit.
- Every `git push` to `main` redeploys the app automatically.

## CLI usage

`download_osm.py` also works as a standalone command:

```
python download_osm.py --shapefile extent.shp --features roads,buildings --out osm.gpkg
python download_osm.py --kml Site.kml --buffer 500
python download_osm.py --area "Shiggaon, Karnataka, India" --features none --tags "amenity=school,natural=tree"
```
