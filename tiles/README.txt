HamLink Radio - Offline Map Tiles
==================================

You normally do not need to do anything here. Open HamLink, go to
Settings -> Offline map, choose the area around home, and press
"Download map". HamLink fetches the tiles from the USGS National Map
(United States public domain) and saves them in this folder as
<name>.mbtiles, then selects the map for the Offline Map tab.

Sizes: a 150 km radius at zoom 12 is roughly 2,000 tiles / 45 MB.
Zoom 14 (street names) is about 16x larger.

You can also drop an .mbtiles file made with another tool (MOBAC, QGIS,
tilemaker, etc.) into this folder; it appears under "Maps on this
computer" in Settings with a Use button. Raster tiles (PNG/JPEG) only.

The map works fully offline - no internet connection is needed to view it.
