# raytracer-demo

A database draws a 3D picture: the SereneDB logo, our name and our investors, ray traced by
**one SQL query** running inside SereneDB, live on the laptop.

![the picture](images/serenedb_investors.png)

## Why it's cool (for the pitch)

> This picture was drawn by a database. There is no graphics card or 3D engine involved: it is
> one SQL query where every pixel is a row, and SereneDB traces the light rays across every
> core of this laptop. ClickHouse wrote a ray tracer like this to show off its engine. DuckDB
> can't run it at all, and CedarDB runs it 33 times slower than ClickHouse. SereneDB is built on
> the DuckDB engine, which we have forked and hardened, and it now runs this kind of query faster
> than ClickHouse, on ClickHouse's own showcase. It's the same engine that runs our customers'
> analytics, search and vector queries.

## Instructions for the meeting

You need: the laptop, **Docker Desktop** installed, and this folder.

**Before the meeting** (with internet, takes ~5 minutes, do it once):

1. Open **Docker Desktop** and wait until it says it's running.
2. Open the **Terminal** app.
3. Type `cd ` (with a space), drag this folder into the Terminal window, press **Enter**.
4. Type `./demo.sh prepare` and press **Enter**. Wait for **"All good. You are ready for the demo."**

**In the meeting** (no internet needed):

1. Make sure **Docker Desktop** is running.
2. In the Terminal (in this folder, see step 3 above), type one of these and press **Enter**:
   - `./demo.sh` — SereneDB draws the picture (about N seconds), and it opens by itself.
   - `./demo.sh race` — SereneDB draws it, then ClickHouse draws the same picture with the same
     query, and the Terminal shows who was faster. Both pictures open at the end.

If anything goes wrong, the finished picture is in the `images` folder: just open it.

After the meeting: `./demo.sh stop` frees the laptop's memory.

## How it works

`gen.py` writes the query. It is the same scene for SereneDB and for ClickHouse; only the SQL
spelling differs (macros vs `WITH` lambdas, `list_transform` vs `arrayMap`, ...):

- every `(pixel, sample)` is a row of `range(width * height * samples)`;
- the vectors are tuples of three doubles;
- a ray's bounces are a fold (`list_reduce` / `arrayFold`) over `range(depth)` whose state is the
  ray: origin, direction, throughput, collected color;
- the letters are spheres on a grid; a ray only tests the grid cells it actually passes through;
- `GROUP BY pixel` averages the samples, and `png.py` turns the `pixel, r, g, b` rows into a PNG.

```bash
python3 gen.py --dialect serenedb --layout banner --width 1920 --height 1080 --samples 4 > scene.sql
```

Knobs for `demo.sh` (environment variables): `WIDTH`, `HEIGHT`, `SAMPLES`, `LAYOUT`
(`banner` or `investors`), `SERENEDB_IMAGE`, `CLICKHOUSE_IMAGE`.
