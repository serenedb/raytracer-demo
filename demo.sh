#!/bin/bash
# SereneDB ray tracer demo. See README.md.
#
#   ./demo.sh prepare    download everything and do a test run (do this BEFORE the meeting)
#   ./demo.sh            render the picture with SereneDB and open it
#   ./demo.sh race       render the same picture with SereneDB, then with ClickHouse
#   ./demo.sh stop       stop the database containers
set -e
cd "$(dirname "$0")"

SERENEDB_IMAGE="${SERENEDB_IMAGE:-serenedb/serenedb:latest}"
CLICKHOUSE_IMAGE="${CLICKHOUSE_IMAGE:-clickhouse/clickhouse-server:26.9}"
WIDTH="${WIDTH:-1280}"
HEIGHT="${HEIGHT:-720}"
SAMPLES="${SAMPLES:-2}"
LAYOUT="${LAYOUT:-banner}"
OUT=out
mkdir -p "$OUT"

say() { printf '\033[1;35m%s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m%s\033[0m\n' "$*"; exit 1; }
now_ms() { python3 -c 'import time; print(int(time.time() * 1000))'; }

check_docker() {
  command -v docker >/dev/null || fail "Docker is not installed. Install Docker Desktop first."
  docker info >/dev/null 2>&1 || fail "Docker is not running. Open Docker Desktop, wait until it says 'running', then try again."
}

start_serenedb() {
  if ! docker ps --format '{{.Names}}' | grep -qx sdb-demo; then
    docker rm -f sdb-demo >/dev/null 2>&1 || true
    docker run -d --name sdb-demo -e SERENEDB_HOST_AUTH_METHOD=trust "$SERENEDB_IMAGE" >/dev/null
  fi
  for _ in $(seq 60); do
    docker exec sdb-demo psql -h 127.0.0.1 -p 7890 -U postgres -d postgres -Atc 'SELECT 1' >/dev/null 2>&1 && return
    sleep 1
  done
  fail "SereneDB did not start."
}

start_clickhouse() {
  if ! docker ps --format '{{.Names}}' | grep -qx ch-demo; then
    docker rm -f ch-demo >/dev/null 2>&1 || true
    docker run -d --name ch-demo --ulimit nofile=262144:262144 "$CLICKHOUSE_IMAGE" >/dev/null
  fi
  for _ in $(seq 60); do
    docker exec ch-demo clickhouse-client -q 'SELECT 1' >/dev/null 2>&1 && return
    sleep 1
  done
  fail "ClickHouse did not start."
}

# render ENGINE: writes $OUT/ENGINE.png and prints the time
render() {
  local engine=$1 sql="$OUT/$1.sql" tsv="$OUT/$1.tsv" start end
  python3 gen.py --dialect "$engine" --layout "$LAYOUT" --width "$WIDTH" --height "$HEIGHT" --samples "$SAMPLES" > "$sql"
  start=$(now_ms)
  if [ "$engine" = serenedb ]; then
    docker exec -i sdb-demo psql -h 127.0.0.1 -p 7890 -U postgres -d postgres -X -q -At -F "$(printf '\t')" \
      -v ON_ERROR_STOP=1 < "$sql" > "$tsv" || fail "SereneDB render failed."
  else
    docker exec -i ch-demo clickhouse-client --multiquery < "$sql" > "$tsv" || fail "ClickHouse render failed."
  fi
  end=$(now_ms)
  python3 png.py "$WIDTH" "$HEIGHT" "$OUT/$engine.png" < "$tsv"
  echo $((end - start)) > "$OUT/$engine.ms"
}

seconds() { python3 -c "print(f'{$(cat "$OUT/$1.ms") / 1000:.1f}')"; }

show() {
  if command -v open >/dev/null; then open "$@"; else xdg-open "$1" >/dev/null 2>&1 || true; fi
}

case "${1:-render}" in
  prepare)
    check_docker
    say "Downloading SereneDB and ClickHouse (needs internet, takes a few minutes the first time)..."
    docker pull "$SERENEDB_IMAGE"
    docker pull "$CLICKHOUSE_IMAGE"
    start_serenedb
    start_clickhouse
    say "Test run with a small picture..."
    WIDTH=320 HEIGHT=180 SAMPLES=1 render serenedb
    WIDTH=320 HEIGHT=180 SAMPLES=1 render clickhouse
    say "All good. You are ready for the demo."
    ;;
  render)
    check_docker
    start_serenedb
    say "SereneDB is drawing the picture with one SQL query, using every core of this laptop..."
    render serenedb
    say "Done in $(seconds serenedb) seconds."
    show "$OUT/serenedb.png"
    ;;
  race)
    check_docker
    start_serenedb
    start_clickhouse
    say "1/2  SereneDB is drawing the picture..."
    render serenedb
    say "     SereneDB:   $(seconds serenedb) s"
    say "2/2  ClickHouse is drawing the same picture with the same SQL..."
    render clickhouse
    say "     ClickHouse: $(seconds clickhouse) s"
    python3 -c "s, c = $(cat "$OUT/serenedb.ms"), $(cat "$OUT/clickhouse.ms"); print(f'\033[1;32mSereneDB is {c / s:.1f}x faster than ClickHouse\033[0m' if s < c else f'ClickHouse was faster this time ({s / c:.1f}x)')"
    show "$OUT/serenedb.png" "$OUT/clickhouse.png"
    ;;
  stop)
    docker rm -f sdb-demo ch-demo >/dev/null 2>&1 || true
    say "Stopped."
    ;;
  *)
    sed -n '2,9p' "$0"
    exit 1
    ;;
esac
