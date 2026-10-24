Self-hosted interface typography

Manrope 4.505, variable weight 200–800
Source: https://github.com/google/fonts/tree/main/ofl/manrope
Original: https://raw.githubusercontent.com/google/fonts/main/ofl/manrope/Manrope%5Bwght%5D.ttf
Original SHA-256: 3ae11c49db0455a3cc33e37d380f20fdb8c7f8b41dc07625c177e3d87a9d6ae6
WOFF2 SHA-256: 3808b5edc88040b291cee90a79a630838ae401b09ec6ee9219770cef60243112
License: Manrope-OFL.txt (SIL Open Font License 1.1)

Newsreader 1.003, variable weight 200–800, optical size 6–72
Source: https://github.com/google/fonts/tree/main/ofl/newsreader
Original: https://raw.githubusercontent.com/google/fonts/main/ofl/newsreader/Newsreader%5Bopsz,wght%5D.ttf
Original SHA-256: 8a08d13f8a6c0d51be379a60af84f945f65369a67e509ee3c3bdcc421254d7c1
WOFF2 SHA-256: d1d0050f48c3e0b2433312e08834ee34e82f809a16ea2c43363e75b6b238480c
License: Newsreader-OFL.txt (SIL Open Font License 1.1)

Downloaded from the upstream Google Fonts repository on 2026-10-15. Original
TTF files are included alongside WOFF2 files. WOFF2 conversion uses fontTools
TTFont with flavor='woff2'; no glyphs, styles, names, or font axes were removed.
There are no external font requests at runtime. The WOFF2 bytes are bundled in
the existing public login.js asset; FontFace loads them directly from memory.
demo.js shares that loader, so fonts are decoded only once per document.

Interface icons must use SVG rather than unsupported Unicode symbols: Manrope
does not include checkmark/cross pictograms; Newsreader does not include arrows.
