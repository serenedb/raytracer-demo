#!/bin/bash
# Render the LinkedIn milestone picture with SereneDB:
#   linkedin/milestone.sql   the query, as SereneDB ran it
#   linkedin/milestone.png   the picture it returned
# The number and the line under it are MILESTONE in gen.py; WIDTH, HEIGHT, SAMPLES as in demo.sh.
set -e
cd "$(dirname "$0")/.."

W="${WIDTH:-1440}" H="${HEIGHT:-1440}" S="${SAMPLES:-6}"
docker ps --format '{{.Names}}' | grep -qx sdb-demo || ./demo.sh prepare

python3 gen.py --scene milestone --width "$W" --height "$H" --samples "$S" > linkedin/milestone.sql
start=$(date +%s)
docker exec -i sdb-demo psql -h 127.0.0.1 -p 7890 -U postgres -d postgres -X -q -At -F "$(printf '\t')" \
  -v ON_ERROR_STOP=1 < linkedin/milestone.sql | python3 png.py "$W" "$H" linkedin/milestone.png
echo "linkedin/milestone.png ($(( $(date +%s) - start )) s)"
