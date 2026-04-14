HamLink Radio — Offline Map Tiles
==================================

Place your .mbtiles file in this folder. The filename should match
your state name in lowercase with hyphens for spaces.

Examples:
  georgia.mbtiles
  new-york.mbtiles
  north-carolina.mbtiles

How to download tiles:
  1. Download MOBAC (Mobile Atlas Creator): https://mobac.sourceforge.io/
  2. Open MOBAC and select "OpenStreetMap" as the map source
  3. Select your state area on the map
  4. Choose zoom levels 6-13 (recommended for state-level coverage)
  5. Set output format to "MBTiles (SQLite)"
  6. Click "Create Atlas"
  7. Move the generated .mbtiles file to this folder
  8. In HamLink Settings, select your state from the Offline Map dropdown

Typical file sizes:
  Small state (Rhode Island): ~20 MB
  Medium state (Georgia): ~100-200 MB
  Large state (Texas): ~300-500 MB

The map works fully offline — no internet connection needed to view it.
