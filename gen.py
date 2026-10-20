#!/usr/bin/env python3
"""Emit one SQL query that ray traces the demo scene, for SereneDB or ClickHouse.

Every (pixel, sample) is a row; the light path of a row is a fold over
range(depth) whose accumulator is the ray state, so rows stay independent and the
render runs on all cores. The query returns `pixel, r, g, b` (0-255); png.py turns
that into an image.

Both dialects get the same scene and the same arithmetic: only the spelling of a
lambda, a let-binding, a tuple element or a list function differs.
"""
import argparse
import itertools
import math
import random
import re
import sys

import wordmark

CH = False
_ids = itertools.count()


# ---- dialect ------------------------------------------------------------------

def rename(body, old, new):
    return re.sub(rf"(?<![\w$.]){re.escape(old)}\b", new, body)


def let(binds, body):
    """Bind each (name, expr) once, by value: a lambda over a one-element list."""
    for name, expr in reversed(binds):
        var = f"{name}_{next(_ids)}"
        body = rename(body, name, var)
        body = (f"arrayMap({var} -> {body}, [{expr}])[1]" if CH
                else f"list_transform([{expr}], {var} -> {body})[1]")
    return body


def amap(name, arr, body):
    var = f"{name}_{next(_ids)}"
    body = rename(body, name, var)
    return f"arrayMap({var} -> {body}, {arr})" if CH else f"list_transform({arr}, {var} -> {body})"


def flatten(a):
    return f"arrayFlatten({a})" if CH else f"flatten({a})"


def amin(a):
    return f"arrayMin({a})" if CH else f"list_min({a})"


def push(a, x):
    return f"arrayPushBack({a}, {x})" if CH else f"list_append({a}, {x})"


def index_of(a, x):
    return f"indexOf({a}, {x})" if CH else f"coalesce(list_position({a}, {x}), 0)"


def el(t, i):
    return f"tupleElement({t}, {i})" if CH else f"({t})[{i}]"


def idiv(a, b):
    return f"intDiv({a}, {b})" if CH else f"(({a}) // ({b}))"


def floori(x):
    return f"toInt64(floor({x}))" if CH else f"CAST(floor({x}) AS BIGINT)"


def num(x):
    s = f"{float(x):.7g}"
    if "e" in s:
        s = f"{float(x):.10f}".rstrip("0")
    return s if "." in s else s + ".0"


def vec(v):
    return "(" + ", ".join(num(c) for c in v) + ")"


CONSTS = []
MACROS = []


def const(name, sql):
    CONSTS.append((name, sql))


def macro(name, params, body):
    for p in params:
        body = rename(body, p, f"{p}_{name}")
    MACROS.append((name, [f"{p}_{name}" for p in params], body))


# ---- vector algebra -------------------------------------------------------------

def define_vectors():
    if CH:
        macro("va", ["a", "b"], "tuplePlus(a, b)")
        macro("vs", ["a", "b"], "tupleMinus(a, b)")
        macro("vm", ["a", "k"], "tupleMultiplyByNumber(a, k)")
        macro("vmul", ["a", "b"], "tupleMultiply(a, b)")
        macro("vd", ["a", "b"], "dotProduct(a, b)")
        macro("vlen", ["a"], "L2Norm(a)")
        macro("vn", ["a"], "L2Normalize(a)")
        macro("vx", ["a", "b"], f"({el('a', 2)} * {el('b', 3)} - {el('a', 3)} * {el('b', 2)}, "
                                f"{el('a', 3)} * {el('b', 1)} - {el('a', 1)} * {el('b', 3)}, "
                                f"{el('a', 1)} * {el('b', 2)} - {el('a', 2)} * {el('b', 1)})")
    else:
        x = lambda i: el("x", i)
        y = lambda i: el("y", i)
        macro("va", ["a", "b"], let([("x", "a"), ("y", "b")], f"({x(1)} + {y(1)}, {x(2)} + {y(2)}, {x(3)} + {y(3)})"))
        macro("vs", ["a", "b"], let([("x", "a"), ("y", "b")], f"({x(1)} - {y(1)}, {x(2)} - {y(2)}, {x(3)} - {y(3)})"))
        macro("vm", ["a", "k"], let([("x", "a"), ("y", "k")], f"({x(1)} * y, {x(2)} * y, {x(3)} * y)"))
        macro("vmul", ["a", "b"], let([("x", "a"), ("y", "b")], f"({x(1)} * {y(1)}, {x(2)} * {y(2)}, {x(3)} * {y(3)})"))
        macro("vd", ["a", "b"], let([("x", "a"), ("y", "b")], f"{x(1)} * {y(1)} + {x(2)} * {y(2)} + {x(3)} * {y(3)}"))
        macro("vlen", ["a"], let([("x", "a")], f"sqrt({x(1)} * {x(1)} + {x(2)} * {x(2)} + {x(3)} * {x(3)})"))
        macro("vn", ["a"], let([("x", "a"), ("y", "vlen(x)")], f"({x(1)} / y, {x(2)} / y, {x(3)} / y)"))
        macro("vx", ["a", "b"], let([("x", "a"), ("y", "b")], f"({x(2)} * {y(3)} - {x(3)} * {y(2)}, "
                                                              f"{x(3)} * {y(1)} - {x(1)} * {y(3)}, "
                                                              f"{x(1)} * {y(2)} - {x(2)} * {y(1)})"))
    macro("vref", ["d", "n"], "vs(d, vm(n, 2.0 * vd(d, n)))")


# ---- scene ----------------------------------------------------------------------

def srgb(hex_color):
    """'#895AF8' -> linear RGB, since the image is gamma-encoded at the end."""
    c = [int(hex_color[i:i + 2], 16) / 255.0 for i in (1, 3, 5)]
    return tuple(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in c)


PURPLE, BLUE = srgb("#895AF8"), srgb("#80BEFF")
LOGO_BLUE = srgb("#5FA8FF")   # a touch deeper: the filmic curve lifts it back

#            base color          kd    kr    tint                  spec
MATERIALS = {
    "violet":  (PURPLE,             0.75, 0.35, (0.85, 0.80, 1.0),  1.0),
    "logo":    (PURPLE,             0.90, 0.0,  (1.0, 1.0, 1.0),    0.6),
    # A mirror with a cold tint: it shows the whole scene back.
    "chrome":  ((0.85, 0.86, 0.92), 0.20, 0.82, (0.95, 0.96, 1.0),  1.0),
    "pearl":   ((0.82, 0.82, 0.88), 0.80, 0.22, (1.0, 1.0, 1.0),    0.9),
    # The stars: bright, a little glossy, faceted by their flat triangles.
    "star":    ((0.95, 0.94, 1.0),  0.85, 0.25, (1.0, 1.0, 1.0),    1.0),
}
THEMES = {
    # haze (the far floor) is the horizon color, so floor and sky meet without a seam.
    "light": dict(floor="#F4F1FF", grid="#B9A8F2", floor_kr=0.25, horizon="#E9E3FF", zenith="#9C8BE6",
                  amb=0.30, shadow=0.3, exposure=0.95),
    "dark":  dict(floor="#0E0B22", grid="#5A43C9", floor_kr=0.45, horizon="#24195E", zenith="#05040F",
                  amb=0.10, shadow=1.0, exposure=1.6),
}
THEME = THEMES["dark"]
# The wordmark: bold, wide tubes, like the SereneDB logo's lettering.
TITLE = dict(tube=0.3, cell=0.8)
# The planet floats over the middle of the wordmark: its radius is a fraction of the
# wordmark's width, `gap` is the air between them. Everything else is placed in planet
# radii from its centre: x to the right, y away from the camera, z up.
PLANET = dict(title_z=3.0, title_size=2.2, radius=0.42, gap=12.0)
# Moons around the planet: (x, y, z) of the centre, radius, material.
MOONS = [
    ((-2.45, 0.30, 0.80), 0.24, "chrome"),
    ((2.40, -0.10, 1.00), 0.15, "pearl"),
    ((1.55, -1.10, -1.05), 0.09, "pearl"),
]
# Balls resting on the floor: (x, y) in planet radii from under its centre, radius, material.
FLOOR_BALLS = [
    ((-2.15, -0.30), 0.36, "chrome"),
    ((2.05, -0.75), 0.27, "pearl"),
    ((-1.25, -1.55), 0.14, "pearl"),
    ((1.25, -1.70), 0.12, "chrome"),
    ((-3.05, 0.90), 0.20, "pearl"),
    ((3.10, 0.70), 0.30, "chrome"),
]
# Solid five-pointed stars flying around the planet, placed by a seeded shuffle so the
# picture is the same on every run: how many, the range of their radii, and the box they
# fly in (x, y, z ranges), all in planet radii from its centre. A star never sits in front
# of the planet's face, nor too close to another object.
STARS = dict(count=26, seed=11, size=(0.08, 0.28), box=((-4.4, 4.4), (-1.6, 5.0), (-0.55, 2.15)))
# How much bigger than the scene the picture is, in each direction: the rest is sky and floor.
FRAME = 1.25

TILE = 8.0
GRID_LINE = 0.035   # half-width of a floor grid line, as a fraction of a tile


class Wordmark:
    """The SereneDB wordmark in tubes, standing on the y = y0 plane, its baseline at z_bottom.

    The segments are binned into square cells of the text plane, so a ray tests only
    the tubes in the cells it passes through (cells holds offsets into ids, CSR style).
    """

    def __init__(self, x0, z_bottom, y0=0.0, size=1.0, tube=0.42, mat="pearl", cell=2.0):
        segs, w, n_outline = wordmark.layout()
        self.cell_units = cell
        self.width = w * size
        self.height = wordmark.CAP * size
        self.x0, self.zb, self.y0, self.size, self.mat = x0, z_bottom, y0, size, mat
        self.r = tube * size
        self.segs = segs
        # Fill tubes get a flat-faced normal, so a filled letter is not striped.
        self.flat = [0 if j < n_outline else 1 for j in range(len(segs))]

    @property
    def zt(self):
        return self.zb + self.height

    def world(self):
        k = self.size
        return [((self.x0 + p[0] * k, self.zb + p[1] * k), (self.x0 + q[0] * k, self.zb + q[1] * k)) for p, q in self.segs]

    def grid(self):
        segs = self.world()
        r = self.r
        self.gx0 = min(min(p[0], q[0]) for p, q in segs) - r
        self.gz0 = min(min(p[1], q[1]) for p, q in segs) - r
        gx1 = max(max(p[0], q[0]) for p, q in segs) + r
        gz1 = max(max(p[1], q[1]) for p, q in segs) + r
        self.cell = self.cell_units * self.size
        self.ncx = int((gx1 - self.gx0) / self.cell) + 1
        self.ncz = int((gz1 - self.gz0) / self.cell) + 1
        cells = [[] for _ in range(self.ncx * self.ncz)]
        for i, (p, q) in enumerate(segs):
            for cz in range(int((min(p[1], q[1]) - r - self.gz0) / self.cell), int((max(p[1], q[1]) + r - self.gz0) / self.cell) + 1):
                for cx in range(int((min(p[0], q[0]) - r - self.gx0) / self.cell), int((max(p[0], q[0]) + r - self.gx0) / self.cell) + 1):
                    if seg_box_dist(p, q, self.gx0 + cx * self.cell, self.gz0 + cz * self.cell, self.cell) <= r:
                        cells[cz * self.ncx + cx].append(i)
        start, ids = [], []
        for c in cells:
            start.append(len(ids))
            ids += c
        return segs, start, [len(c) for c in cells], ids


def seg_box_dist(p, q, bx, bz, size):
    """Distance from segment pq to the square [bx, bx + size] x [bz, bz + size], sampled."""
    best = 1e9
    n = max(16, int(math.hypot(q[0] - p[0], q[1] - p[1]) / (0.05 * size)))
    for i in range(n + 1):
        t = i / n
        x, z = p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t
        dx = max(bx - x, 0.0, x - bx - size)
        dz = max(bz - z, 0.0, z - bz - size)
        best = min(best, math.hypot(dx, dz))
    return best


class Ball:
    def __init__(self, c, r, mat):
        self.c, self.r, self.mat = c, r, mat


class Star:
    """A solid five-pointed star: ten points round its rim, alternately out and in, and an
    apex on each face, so twenty flat triangles. It lies in the x-z plane facing the
    camera, then is turned by (yaw, pitch, roll) and moved to c."""

    TRIANGLES = 20

    def __init__(self, c, size, turn, mat="star", inner=0.45, depth=0.42):
        self.c, self.r, self.mat = c, size, mat
        yaw, pitch, roll = (math.radians(a) for a in turn)
        rz = ((math.cos(yaw), -math.sin(yaw), 0.0), (math.sin(yaw), math.cos(yaw), 0.0), (0.0, 0.0, 1.0))
        rx = ((1.0, 0.0, 0.0), (0.0, math.cos(pitch), -math.sin(pitch)), (0.0, math.sin(pitch), math.cos(pitch)))
        ry = ((math.cos(roll), 0.0, math.sin(roll)), (0.0, 1.0, 0.0), (-math.sin(roll), 0.0, math.cos(roll)))
        m = matmul(rz, matmul(rx, ry))
        at = lambda v: tuple(c[i] + sum(m[i][j] * v[j] for j in range(3)) for i in range(3))
        rim = []
        for k in range(10):
            a = math.pi / 2 + k * math.pi / 5
            r = size if k % 2 == 0 else size * inner
            rim.append(at((r * math.cos(a), 0.0, r * math.sin(a))))
        front, back = at((0.0, -depth * size, 0.0)), at((0.0, depth * size, 0.0))
        self.tris = []
        for k in range(10):
            p, q = rim[k], rim[(k + 1) % 10]
            self.tris += [(p, q, front), (q, p, back)]


def place_stars(planet, moons, R):
    rng = random.Random(STARS["seed"])
    (xa, xb), (ya, yb), (za, zb) = STARS["box"]
    lo, hi = STARS["size"]
    stars = []
    while len(stars) < STARS["count"]:
        x, y, z = rng.uniform(xa, xb), rng.uniform(ya, yb), rng.uniform(za, zb)
        r = lo + (hi - lo) * rng.random() ** 2   # mostly small, a few big
        c = (planet.c[0] + x * R, y * R, planet.c[2] + z * R)
        # Not over the planet's face, as the camera sees it.
        if abs(x) < 1.35 and abs(z) < 1.35 and y < 1.0:
            continue
        others = [planet] + moons + stars
        if any(math.dist(c, o.c) < o.r + r * R + 0.25 * R for o in others):
            continue
        # Mostly facing the camera, turned enough that the facets catch the light.
        turn = (rng.uniform(-50, 50), rng.uniform(-40, 40), rng.uniform(0, 72))
        stars.append(Star(c, r * R, turn))
    return stars


def matmul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def layout():
    """The SereneDB mark as a planet in the middle of the picture, moons and solid stars
    flying around it, the name on the floor under it, and balls resting on that floor.

    Returns the objects, the box the picture must hold (the wordmark's reflection
    included) and the height the camera aims at: the planet's centre.
    """
    title = Wordmark(x0=0.0, z_bottom=PLANET["title_z"], size=PLANET["title_size"], mat="violet", **TITLE)
    R = PLANET["radius"] * title.width
    cx = title.width / 2
    cz = title.zt + PLANET["gap"] + R
    planet = Ball((cx, 0.0, cz), R, "logo")
    moons = [Ball((cx + x * R, y * R, cz + z * R), r * R, m) for (x, y, z), r, m in MOONS]
    floor = [Ball((cx + x * R, y * R, r * R), r * R, m) for (x, y), r, m in FLOOR_BALLS]
    stars = place_stars(planet, moons, R)
    balls = [planet] + moons + floor
    # The picture is framed on the balls; stars may fly off its edges.
    x0 = min(b.c[0] - b.r for b in balls)
    x1 = max(b.c[0] + b.r for b in balls)
    z1 = max(b.c[2] + b.r for b in balls)
    return [title], balls, stars, (x0, x1, -title.zt, z1), cz


# ---- the query --------------------------------------------------------------------

def build(args):
    texts, balls, stars, (x0, x1, z0, z1), aim = layout()
    mats = list(MATERIALS)
    mid = {m: i + 1 for i, m in enumerate(mats)}   # 0 is the floor

    # Camera: centred on the planet, far enough back that the box fits with room
    # around it for the sky, looking a little down.
    aspect = args.width / args.height
    fov = math.radians(args.fov)
    th = math.tan(fov / 2)
    w, h = (x1 - x0) * 1.06, 2 * max(z1 - aim, aim - z0) * FRAME
    dist = max(h / 2 / th, w / 2 / (th * aspect))
    target = ((x0 + x1) / 2, 0.0, aim)
    elev = math.radians(args.elevation)
    eye = (target[0], -dist * math.cos(elev), target[2] + dist * math.sin(elev))
    fwd = norm(sub(target, eye))
    right = norm(cross(fwd, (0.0, 0.0, 1.0)))
    up = cross(right, fwd)
    span = max(x1 - x0, z1 - z0)
    # Near the camera and a little above and left of it, so the shadows fall behind the
    # objects rather than on the wordmark under them.
    light = (x0 + 0.3 * (x1 - x0), eye[1] * 0.8, eye[2] + 0.12 * span)

    const("eps", "0.0005")
    const("eye", vec(eye))
    const("fwd", vec(fwd))
    const("right", vec(vm_(right, th * aspect)))
    const("up", vec(vm_(up, th)))
    const("light", vec(light))
    const("lightR", num(0.03 * span))
    const("amb", num(THEME["amb"]))
    const("shadow", num(THEME["shadow"]))
    const("shin", "70.0")
    const("horizon", vec(srgb(THEME["horizon"])))
    const("camR", vec(right))
    const("camU", vec(up))
    const("camF", vec(fwd))
    const("zenith", vec(srgb(THEME["zenith"])))
    const("haze", vec(srgb(THEME["horizon"])))
    const("fogStart", num(dist * 0.9))
    const("fogRange", num(dist * 2.5))
    const("exposure", num(THEME["exposure"]))

    define_vectors()
    macro("sphT", ["p", "d", "rr"], let(
        [("b", "vd(p, d)"), ("disc", "b * b - (vd(p, p) - rr)")],
        "if(disc > 0.0, if(-b - sqrt(greatest(disc, 0.0)) > $eps, -b - sqrt(greatest(disc, 0.0)), 1e9), 1e9)"))
    macro("planeT", ["o", "d"], f"if({el('d', 3)} < -0.000001 AND {el('o', 3)} > 0.0, -{el('o', 3)} / least({el('d', 3)}, -0.000001), 1e9)")
    # A portable hash, so both engines jitter the samples identically.
    macro("rnd", ["a", "b", "c"], let([("x", "sin(a * 12.9898 + b * 78.233 + c * 37.719) * 43758.5453")], "x - floor(x)"))
    # A dark sky with a glow along the horizon.
    macro("sky", ["d"], let([("k", f"exp(-greatest({el('d', 3)}, 0.0) * 14.0)")], "va(vm($horizon, k), vm($zenith, 1.0 - k))"))
    # A polished floor with a thin grid.
    macro("floorCol", ["p"], let([
        ("fx", f"{el('p', 1)} / {num(TILE)} - floor({el('p', 1)} / {num(TILE)})"),
        ("fy", f"{el('p', 2)} / {num(TILE)} - floor({el('p', 2)} / {num(TILE)})"),
        # The lines fade out with distance, before they get thinner than a pixel.
        ("fade", f"greatest(1.0 - vlen(vs(p, $eye)) / {num(dist * 1.6)}, 0.0)"),
        ("line", f"if(least(least(fx, 1.0 - fx), least(fy, 1.0 - fy)) < {num(GRID_LINE)}, fade, 0.0)"),
    ], f"va(vm({vec(srgb(THEME['grid']))}, line), vm({vec(srgb(THEME['floor']))}, 1.0 - line))"))

    # A text block: intersect the ray with the slab |y - y0| <= r that holds the
    # tubes, and test only the capsules binned into the cells under that stretch of ray.
    for k, t in enumerate(texts):
        segs, start, count, ids = t.grid()
        arr = lambda xs: "[" + ", ".join(num(x) for x in xs) + "]"
        const(f"pax{k}", arr(p[0] for p, q in segs))
        const(f"paz{k}", arr(p[1] for p, q in segs))
        const(f"bax{k}", arr(q[0] - p[0] for p, q in segs))
        const(f"baz{k}", arr(q[1] - p[1] for p, q in segs))
        const(f"bb{k}", arr(max((q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2, 1e-6) for p, q in segs))
        const(f"start{k}", "[" + ", ".join(map(str, start)) + "]")
        const(f"count{k}", "[" + ", ".join(map(str, count)) + "]")
        const(f"ids{k}", "[" + ", ".join(map(str, ids)) + "]")
        if any(t.flat):
            const(f"flat{k}", "[" + ", ".join(map(str, t.flat)) + "]")
        y0, r, rr = num(t.y0), num(t.r), num(t.r * t.r)
        seg = [("pa", f"($pax{k}[i + 1], {y0}, $paz{k}[i + 1])"),
               ("ba", f"($bax{k}[i + 1], 0.0, $baz{k}[i + 1])"),
               ("baba", f"$bb{k}[i + 1]")]
        # Ray vs capsule (after Inigo Quilez): the infinite cylinder, then the end caps.
        macro(f"cap{k}", ["o", "d", "i"], let(seg + [
            ("oa", "vs(o, pa)"),
            ("bard", "vd(ba, d)"), ("baoa", "vd(ba, oa)"),
            ("qa", "greatest(baba - bard * bard, 0.000000001)"),
            ("qb", "baba * vd(d, oa) - baoa * bard"),
            ("qc", f"baba * vd(oa, oa) - baoa * baoa - {rr} * baba"),
            ("h", "qb * qb - qa * qc"),
            ("tb", "(-qb - sqrt(greatest(h, 0.0))) / qa"),
            ("yy", "baoa + tb * bard"),
            ("oc", "if(yy <= 0.0, oa, vs(oa, ba))"),
            ("b2", "vd(d, oc)"),
            ("h2", f"b2 * b2 - (vd(oc, oc) - {rr})"),
            ("tc", "-b2 - sqrt(greatest(h2, 0.0))"),
        ], "if(h < 0.0, 1e9, if(yy > 0.0 AND yy < baba, if(tb > $eps, tb, 1e9), if(h2 > 0.0 AND tc > $eps, tc, 1e9)))"))
        flat_n = (f"if($flat{k}[i + 1] = 1, vn(({el('nn', 1)}, {el('nn', 2)}, 0.0)), nn)" if any(t.flat) else "nn")
        macro(f"capN{k}", ["p", "i"], let(seg + [
            ("hh", "least(greatest(vd(vs(p, pa), ba) / baba, 0.0), 1.0)"),
            ("nn", "vn(vs(p, va(pa, vm(ba, hh))))"),
        ], flat_n))
        ox, oy, oz = el("o", 1), el("o", 2), el("o", 3)
        dx, dy, dz = el("d", 1), el("d", 2), el("d", 3)
        cell, gx0, gz0 = num(t.cell), num(t.gx0), num(t.gz0)
        slice_ = (f"arraySlice($ids{k}, $start{k}[c + 1] + 1, $count{k}[c + 1])" if CH
                  else f"list_slice($ids{k}, $start{k}[c + 1] + 1, $start{k}[c + 1] + $count{k}[c + 1])")
        macro(f"text{k}", ["o", "d"], let([
            ("par", f"abs({dy}) < 0.000001"),
            ("dyy", f"if(par, 1.0, {dy})"),
            ("ins", f"abs({oy} - {y0}) <= {r}"),
            ("ta", f"if(par, if(ins, 0.0, 1e9), ({y0} - {r} - {oy}) / dyy)"),
            ("tb", f"if(par, if(ins, 1e5, -1e9), ({y0} + {r} - {oy}) / dyy)"),
            ("t0", "greatest(least(ta, tb), 0.0)"),
            ("t1", "least(greatest(ta, tb), 1e5)"),
            ("xa", f"{ox} + {dx} * t0"), ("xb", f"{ox} + {dx} * t1"),
            ("za", f"{oz} + {dz} * t0"), ("zb", f"{oz} + {dz} * t1"),
            ("cx0", f"greatest({floori(f'(least(xa, xb) - {gx0}) / {cell}')}, 0)"),
            ("cx1", f"least({floori(f'(greatest(xa, xb) - {gx0}) / {cell}')}, {t.ncx - 1})"),
            ("cz0", f"greatest({floori(f'(least(za, zb) - {gz0}) / {cell}')}, 0)"),
            ("cz1", f"if(t1 < t0, -1, least({floori(f'(greatest(za, zb) - {gz0}) / {cell}')}, {t.ncz - 1}))"),
            ("cells", flatten(amap("cz", "range(cz0, cz1 + 1)", amap("cx", "range(cx0, cx1 + 1)", f"cz * {t.ncx} + cx")))),
            ("cands", flatten(amap("c", "cells", slice_))),
            ("ts", amap("i", "cands", f"cap{k}(o, d, i)")),
            ("tm", amin(push("ts", "1e9"))),
        ], f"(tm, if(tm < 1e8, cands[{index_of('ts', 'tm')}], 0))"))

    # The SereneDB mark painted on the side of the ball that faces the camera: a purple
    # disc, a blue ellipse up and to the right, a white one further up-right (from the SVG).
    for b in balls:
        if b.mat == "logo":
            macro("logoCol", ["p"], let([
                ("q", f"vm(vs(p, {vec(b.c)}), {num(1.0 / b.r)})"),
                ("u", "vd(q, $camR)"), ("v", "vd(q, $camU)"),
                ("front", "vd(q, $camF) < 0.0"),
                ("b1", "(u - 0.173 - (v - 0.173)) * 0.70711 / 0.833"),
                ("b2", "(u - 0.173 + (v - 0.173)) * 0.70711 / 0.714"),
                ("w1", "(u - 0.365 - (v - 0.365)) * 0.70711 / 0.417"),
                ("w2", "(u - 0.365 + (v - 0.365)) * 0.70711 / 0.350"),
            ], f"if(front AND w1 * w1 + w2 * w2 <= 1.0, (0.72, 0.72, 0.76), if(front AND b1 * b1 + b2 * b2 <= 1.0, {vec(LOGO_BLUE)}, {vec(PURPLE)}))"))

    # The stars are triangles, again in arrays: a vertex and two edges of each, and its
    # normal. A ray first tests each star's bounding sphere, then only the triangles of the
    # nearest star whose sphere it crosses (Moller-Trumbore).
    tris = [t for st in stars for t in st.tris]
    e1s = [sub(t[1], t[0]) for t in tris]
    e2s = [sub(t[2], t[0]) for t in tris]
    vecs = lambda vs: "[" + ", ".join(vec(v) for v in vs) + "]"
    const("tv", vecs(t[0] for t in tris))
    const("t1", vecs(e1s))
    const("t2", vecs(e2s))
    const("tn", vecs(norm(cross(a, b)) for a, b in zip(e1s, e2s)))
    const("sc", vecs(st.c for st in stars))
    const("srr", "[" + ", ".join(num((st.r * 1.01) ** 2) for st in stars) + "]")
    macro("triT", ["o", "d", "j"], let([
        ("v0", "$tv[j + 1]"),
        ("e1", "$t1[j + 1]"),
        ("e2", "$t2[j + 1]"),
        ("pv", "vx(d, e2)"),
        ("det", "vd(e1, pv)"),
        ("inv", "1.0 / if(abs(det) < 0.000000001, 0.000000001, det)"),
        ("sv", "vs(o, v0)"),
        ("u", "vd(sv, pv) * inv"),
        ("qv", "vx(sv, e1)"),
        ("w", "vd(d, qv) * inv"),
        ("t", "vd(e2, qv) * inv"),
    ], "if(abs(det) > 0.000000001 AND u >= 0.0 AND u <= 1.0 AND w >= 0.0 AND u + w <= 1.0 AND t > $eps, t, 1e9)"))
    T = Star.TRIANGLES
    macro("starHit", ["o", "d"], let([
        # Where the ray enters each star's bounding sphere, 1e9 if it does not.
        ("bt", amap("i", f"range({len(stars)})", let([
            ("oc", "vs(o, $sc[i + 1])"),
            ("b", "vd(oc, d)"),
            ("disc", "b * b - (vd(oc, oc) - $srr[i + 1])"),
        ], "if(disc > 0.0 AND -b + sqrt(greatest(disc, 0.0)) > 0.0, -b - sqrt(greatest(disc, 0.0)), 1e9)"))),
        ("tb", amin("bt")),
        ("k", f"{index_of('bt', 'tb')} - 1"),
        # Only the nearest of them is tested: the stars do not overlap enough for the one
        # behind to matter, and one star's triangles are one fixed range.
        ("ts", f"if(tb < 1e8, {amap('j', f'range(k * {T}, k * {T} + {T})', 'triT(o, d, j)')}, [1e9])"),
        ("tm", amin("ts")),
    ], f"(tm, if(tm < 1e8, k * {T} + {index_of('ts', 'tm')} - 1, 0))"))

    # Every ball is an entry of three arrays, and one map over them tests them all. One
    # map and not a test per ball, because ClickHouse refuses more than 64 nested lambdas
    # and each let-binding is one: this way the depth does not grow with the scene.
    nums = lambda xs: "[" + ", ".join(num(x) for x in xs) + "]"
    const("bc", "[" + ", ".join(vec(b.c) for b in balls) + "]")
    const("brr", nums(b.r * b.r for b in balls))
    const("bm", "[" + ", ".join(str(mid[b.mat]) for b in balls) + "]")
    macro("ballTs", ["o", "d"], amap("i", f"range({len(balls)})",
                                     "sphT(vs(o, $bc[i + 1]), d, $brr[i + 1])"))

    hits = [f"{el(f'text{k}(o, d)', 1)} < maxd" for k in range(len(texts))]
    hits += [f"{amin('ballTs(o, d)')} < maxd"]
    # The stars cast no shadows. The light is beside the camera, so a star's shadow
    # falls behind it, where the camera does not see it, and testing every shadow ray
    # against their triangles would cost a good part of the render for nothing.
    macro("occluded", ["o", "d", "maxd"], " OR ".join(hits))

    def by_mat(field, default):
        out = default
        for m in reversed(mats):
            out = f"if(m = {mid[m]}, {field(MATERIALS[m])}, {out})"
        return out

    macro("matCol", ["m"], by_mat(lambda m: vec(m[0]), vec((0, 0, 0))))
    macro("matKd", ["m"], by_mat(lambda m: num(m[1]), "1.0"))
    macro("matKr", ["m"], by_mat(lambda m: num(m[2]), num(THEME["floor_kr"])))
    macro("matTint", ["m"], by_mat(lambda m: vec(m[3]), "(1.0, 1.0, 1.0)"))
    macro("matSpec", ["m"], by_mat(lambda m: num(m[4]), "0.0"))

    binds = [("o", el("s", 1)), ("d", el("s", 2)), ("thr", el("s", 3)), ("acc", el("s", 4))]
    objs = []
    for k in range(len(texts)):
        binds.append((f"h{k}", f"text{k}(o, d)"))
        binds.append((f"t{k}", el(f"h{k}", 1)))
        objs.append((f"t{k}", mid[texts[k].mat], f"capN{k}(hp, {el(f'h{k}', 2)})"))
    binds += [("bts", "ballTs(o, d)"), ("tB", amin("bts")), ("iB", index_of("bts", "tB"))]
    objs.append(("tB", "$bm[iB]", "vn(vs(hp, $bc[iB]))"))
    binds += [("hs", "starHit(o, d)"), ("tS", el("hs", 1)), ("jS", el("hs", 2))]
    # Two-sided: a star's facet faces whoever is looking at it.
    sn = "$tn[jS + 1]"
    objs.append(("tS", mid["star"], f"if(vd(d, {sn}) < 0.0, {sn}, vm({sn}, -1.0))"))
    mat_expr, nrm_expr = "0", "(0.0, 0.0, 1.0)"
    for tv, m, n in reversed(objs):
        mat_expr = f"if({tv} = tObj, {m}, {mat_expr})"
        nrm_expr = f"if({tv} = tObj, {n}, {nrm_expr})"
    binds += [
        ("tP", "planeT(o, d)"),
        ("tObj", "least(" + ", ".join(t for t, _, _ in objs) + ")"),
        ("tHit", "least(tObj, tP)"),
        ("miss", "tHit >= 1e8"),
        ("isFloor", "tP <= tObj"),
        ("hp", "va(o, vm(d, tHit))"),
        ("m", f"if(isFloor, 0, {mat_expr})"),
        ("nrm", f"if(isFloor, (0.0, 0.0, 1.0), {nrm_expr})"),
        ("shO", "va(hp, vm(nrm, $eps * 8.0))"),
        ("rdir", "vref(d, nrm)"),
        ("tol", "vs(lpos, hp)"),
        ("distL", "vlen(tol)"),
        ("ldir", "vm(tol, 1.0 / distL)"),
        ("sh", "if(miss, 0.0, if(occluded(shO, ldir, distL), 1.0 - $shadow, 1.0))"),
        ("lam", "greatest(vd(ldir, nrm), 0.0) * sh"),
        ("spec", "pow(greatest(vd(ldir, rdir), 0.0), $shin) * sh * matSpec(m)"),
        ("kr", "matKr(m)"),
        ("base", f"if(isFloor, floorCol(hp), if(m = {mid['logo']}, logoCol(hp), matCol(m)))"),
        ("fog", "least(greatest((tHit - $fogStart) / $fogRange, 0.0), 1.0)"),
        ("loc", "va(vm(base, ($amb + matKd(m) * lam) * (1.0 - kr)), (spec, spec, spec))"),
        ("locF", "va(vm(loc, 1.0 - fog), vm($haze, fog))"),
    ]
    body = ("if(miss, (o, d, thr, va(acc, vmul(thr, sky(d))), 0.0), "
            "(shO, rdir, vm(vmul(thr, matTint(m)), kr * (1.0 - fog)), va(acc, vmul(thr, locF)), if(kr > 0.001, 1.0, 0.0)))")
    macro("bounce", ["s", "lpos"], let(binds, body))

    W, H, S = args.width, args.height, args.samples
    st_type = ("Tuple(Tuple(Float64, Float64, Float64), Tuple(Float64, Float64, Float64), "
               "Tuple(Float64, Float64, Float64), Tuple(Float64, Float64, Float64), Float64)" if CH else
               "STRUCT(o DOUBLE[3], d DOUBLE[3], thr DOUBLE[3], acc DOUBLE[3], alive DOUBLE)")
    jx = "rnd(pixel, sample, 1)" if S > 1 else "0.5"
    jy = "rnd(pixel, sample, 2)" if S > 1 else "0.5"
    lpos = ("va($light, vm((rnd(pixel, sample, 3) - 0.5, rnd(pixel, sample, 4) - 0.5, rnd(pixel, sample, 5) - 0.5), $lightR))"
            if S > 1 else "$light")
    numbers = f"numbers_mt({W * H * S})" if CH else f"(SELECT range AS number FROM range({W * H * S}))"
    fold = ("arrayFold((s, i) -> if(tupleElement(s, 5) > 0.5, bounce(s, lpos), s), range(" + str(args.depth) + "), st0)" if CH
            else "list_reduce(range(" + str(args.depth) + "), (s, i) -> if(s[5] > 0.5, bounce(s, lpos), s), st0)")
    # Exposure with a vignette, the ACES filmic curve, then gamma.
    vig = f"(1.0 - 0.45 * (pow((pixel % {W}) / {num(W)} - 0.5, 2) + pow({idiv('pixel', W)} / {num(H)} - 0.5, 2)))"
    aces = lambda x: f"least(greatest(({x}) * (2.51 * ({x}) + 0.03) / (({x}) * (2.43 * ({x}) + 0.59) + 0.14), 0.0), 1.0)"
    chan = lambda i: f"CAST(round(pow({aces(f'greatest(avg({el(chr(99) + chr(111) + chr(108), i)}), 0.0) * $exposure * {vig}')}, 1.0 / 2.2) * 255.0) AS INTEGER)"
    query = f"""SELECT pixel, {chan(1)} AS r, {chan(2)} AS g, {chan(3)} AS b
FROM (
  SELECT pixel, if({el('st', 5)} > 0.5, va({el('st', 4)}, vmul({el('st', 3)}, sky({el('st', 2)}))), {el('st', 4)}) AS col
  FROM (
    SELECT pixel, {fold} AS st
    FROM (
      SELECT pixel, lpos,
        {cast_state(f"($eye, vn(va($fwd, va(vm($right, ((pixel % {W}) + {jx}) / {num(W)} * 2.0 - 1.0), vm($up, 1.0 - ({idiv('pixel', W)} + {jy}) / {num(H)} * 2.0)))), (1.0, 1.0, 1.0), (0.0, 0.0, 0.0), 1.0)", st_type)} AS st0
      FROM (
        SELECT {idiv('number', S)} AS pixel, number % {S} AS sample, {lpos} AS lpos
        FROM {numbers}
      )
    )
  )
)
GROUP BY pixel"""
    return emit(query)


def cast_state(expr, st_type):
    if CH:
        return f"CAST({expr} AS {st_type})"
    # SereneDB: an unnamed struct of DOUBLE tuples is what the fold carries.
    return expr


def emit(query):
    if CH:
        withs = [f"{sql} AS c_{n}" for n, sql in CONSTS]
        withs += [f"(({', '.join(p)}) -> {body}) AS {n}" for n, p, body in MACROS]
        sql = "SET max_query_size = 100000000;\nWITH\n    " + ",\n    ".join(withs) + "\n" + query
        sql += "\nSETTINGS max_block_size = 2048, optimize_and_compare_chain = 0\nFORMAT TSV;\n"
    else:
        lines = ["SET integer_division = false;"]
        lines += [f"CREATE OR REPLACE MACRO c_{n}() AS {sql};" for n, sql in CONSTS]
        lines += [f"CREATE OR REPLACE MACRO {n}({', '.join(p)}) AS {body};" for n, p, body in MACROS]
        sql = "\n".join(lines) + "\n" + query + ";\n"
    sql = re.sub(r"\$(\w+)", (r"c_\1" if CH else r"c_\1()"), sql)
    if not CH:
        # DuckDB-style engines read 1.5 as DECIMAL; the tracer wants DOUBLE.
        sql = re.sub(r"(?<![\w.])(\d+\.\d+)(?![\w.])", r"\1::DOUBLE", sql)
    return sql


def sub(a, b): return tuple(x - y for x, y in zip(a, b))
def vm_(a, k): return tuple(x * k for x in a)
def norm(a): return vm_(a, 1.0 / math.sqrt(sum(x * x for x in a)))
def cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dialect", choices=["serenedb", "clickhouse"], default="serenedb")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--samples", type=int, default=4)
    p.add_argument("--depth", type=int, default=3)
    p.add_argument("--theme", choices=list(THEMES), default="dark")
    p.add_argument("--fov", type=float, default=30.0)
    p.add_argument("--elevation", type=float, default=3.0, help="camera pitch, degrees")
    args = p.parse_args()
    global CH, THEME
    CH = args.dialect == "clickhouse"
    THEME = THEMES[args.theme]
    sys.stdout.write(build(args))


if __name__ == "__main__":
    main()
