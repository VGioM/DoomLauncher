#!/usr/bin/env bash
# Construit DOOM_Launcher-x86_64.AppImage à partir du dossier AppDir
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
TOOL=./.appimagetool
if [ ! -x "$TOOL" ]; then
    echo "Téléchargement d'appimagetool…"
    curl -fL -o "$TOOL" https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x "$TOOL"
fi
find AppDir -name __pycache__ -prune -exec rm -rf {} +
ARCH=x86_64 "$TOOL" --no-appstream AppDir DOOM_Launcher-x86_64.AppImage
echo "OK : $(pwd)/DOOM_Launcher-x86_64.AppImage"
