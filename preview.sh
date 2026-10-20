#!/bin/bash
# Render every theme with SereneDB and open each picture as soon as it is ready.
#
#   ./preview.sh            quick previews (960x540, 1 sample) into out/preview/
#   ./preview.sh final      the 2560x1440 backups into images/ (about a minute each)
#
# THEMES picks a subset, e.g. THEMES=light ./preview.sh
set -e
cd "$(dirname "$0")"

if [ "$1" = final ]; then
  W=2560 H=1440 S=6 DIR=images
else
  W="${WIDTH:-960}" H="${HEIGHT:-540}" S="${SAMPLES:-1}" DIR=out/preview
fi
THEMES="${THEMES:-dark light}"
mkdir -p "$DIR"

show() {
  if command -v open >/dev/null; then open "$1"; else xdg-open "$1" >/dev/null 2>&1 & fi
}

docker ps --format '{{.Names}}' | grep -qx sdb-demo || ./demo.sh prepare

for T in $THEMES; do
  png="$DIR/serenedb_$T.png"
  start=$(date +%s)
  python3 gen.py --theme "$T" --width "$W" --height "$H" --samples "$S" |
    docker exec -i sdb-demo psql -h 127.0.0.1 -p 7890 -U postgres -d postgres -X -q -At -F "$(printf '\t')" \
      -v ON_ERROR_STOP=1 |
    python3 png.py "$W" "$H" "$png"
  echo "$png ($(( $(date +%s) - start )) s)"
  show "$png"
done
