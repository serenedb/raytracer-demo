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
import re
import sys

import font

INVESTORS = [
    "BALDERTON", "BLACKFIN", "INSIGHT PARTNERS", "DAWN CAPITAL", "CREANDUM",
    "LEFT LANE PARTNERS", "EURAZEO", "PARTECH", "VENTO", "FORTINO",
    "SYNDICATEONE", "6 DEGREES",
]

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
    s = repr(float(x))
    return s if "e" not in s else f"{x:.12f}"


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
    macro("vref", ["d", "n"], "vs(d, vm(n, 2.0 * vd(d, n)))")


# ---- scene ----------------------------------------------------------------------

def srgb(hex_color):
    """'#895AF8' -> linear RGB, since the image is gamma-encoded at the end."""
    c = [int(hex_color[i:i + 2], 16) / 255.0 for i in (1, 3, 5)]
    return tuple(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in c)


PURPLE, BLUE = srgb("#895AF8"), srgb("#80BEFF")

#            base color          kd    kr    tint                  spec
MATERIALS = {
    "chrome":  ((0.02, 0.02, 0.03), 0.20, 0.78, (0.92, 0.93, 0.97), 1.0),
    "pearl":   ((0.80, 0.80, 0.86), 0.85, 0.22, (1.0, 1.0, 1.0),    0.8),
    "violet":  (PURPLE,             0.90, 0.18, (1.0, 1.0, 1.0),    0.7),
    "purple":  (PURPLE,             0.90, 0.10, (1.0, 1.0, 1.0),    0.45),
    "blue":    (BLUE,               0.90, 0.10, (1.0, 1.0, 1.0),    0.45),
    "white":   ((0.92, 0.92, 0.95), 0.90, 0.08, (1.0, 1.0, 1.0),    0.45),
}
THEMES = {
    "light": dict(floor_a="#E9E2FF", floor_b="#FFFFFF", floor_kr=0.18, horizon="#F3EEFF", zenith="#6E55C8",
                  haze="#F1ECFF", amb=0.22, text="chrome"),
    "dark":  dict(floor_a="#1B1636", floor_b="#2A2352", floor_kr=0.35, horizon="#3A2C7A", zenith="#07051A",
                  haze="#2A2160", amb=0.10, text="pearl"),
}
THEME = THEMES["dark"]
FLOOR = {"tile": 12.0}


class Text:
    """Lines of text standing on the y = y0 plane; row 0 is the top line."""

    def __init__(self, lines, x0, z_bottom, y0=0.0, sp=1.0, radius=0.6, mat="chrome", align="left", line_gap=3):
        banners = [font.banner(t) for t in lines]
        self.nc = max(w for _, w in banners)
        rows = []
        for i, (b, w) in enumerate(banners):
            pad = (self.nc - w) // 2 if align == "center" else 0
            rows += ["." * pad + r.ljust(self.nc - pad, ".") for r in b]
            if i + 1 < len(banners):
                rows += ["." * self.nc] * line_gap
        self.rows, self.nr = rows, len(rows)
        self.sp, self.r, self.y0, self.mat = sp, radius * sp, y0, mat
        self.x0 = x0
        self.zt = z_bottom + (self.nr - 1) * sp
        self.width, self.height = (self.nc - 1) * sp, (self.nr - 1) * sp

    def bits(self):
        return "[" + ",".join("1" if c == "#" else "0" for r in self.rows for c in r) + "]"


class Ball:
    def __init__(self, c, r, mat):
        self.c, self.r, self.mat = c, r, mat


def layout(name):
    texts, balls = [], []
    if name == "investors":
        # Investors on the left, the SereneDB mark and name on the right.
        inv = Text(INVESTORS, x0=0.0, z_bottom=4.0, sp=1.0, radius=0.62, mat=THEME["text"], line_gap=3)
        texts.append(inv)
        R = 0.36 * inv.height
        cx = inv.width + 0.18 * inv.height + R
        name_sp = 1.6
        sdb = Text(["SERENEDB"], x0=0.0, z_bottom=4.0, sp=name_sp, radius=0.72, mat="violet")
        sdb.x0 = cx - sdb.width / 2
        texts.append(sdb)
        cz = sdb.zt + sdb.r + 0.06 * inv.height + R
        balls += logo((cx, 0.35 * R, cz), R)
        bbox = (0.0, cx + R, 0.0, max(inv.zt, cz + R))
    elif name == "banner":
        # The SereneDB mark and name on top, the investors in three columns below.
        cols = [INVESTORS[i::3] for i in range(3)]
        x, colw = 0.0, []
        for col in cols:
            t = Text(col, x0=x, z_bottom=4.0, sp=1.0, radius=0.62, mat=THEME["text"], align="center")
            texts.append(t)
            x += t.width + 12.0
        total = x - 12.0
        top = texts[0].zt + 16.0
        sdb = Text(["SERENEDB"], x0=0.0, z_bottom=top, sp=2.6, radius=0.72, mat="violet")
        R = 0.95 * sdb.height
        sdb.x0 = (total - sdb.width + 2.4 * R) / 2
        texts.append(sdb)
        balls += logo((sdb.x0 - 1.4 * R, 0.6 * R, top + sdb.height / 2), R)
        bbox = (0.0, total, 0.0, top + sdb.height / 2 + R)
    else:
        sys.exit(f"unknown layout {name}")
    return texts, balls, bbox


def logo(c, R):
    """The SereneDB mark: a purple sphere, a blue one bulging out of it toward the
    viewer and up-right, and a white highlight sphere bulging out of the blue one."""
    cx, cy, cz = c
    return [
        Ball(c, R, "purple"),
        Ball((cx + 0.22 * R, cy - 0.42 * R, cz + 0.17 * R), 0.70 * R, "blue"),
        Ball((cx + 0.42 * R, cy - 0.78 * R, cz + 0.36 * R), 0.32 * R, "white"),
    ]


# ---- the query --------------------------------------------------------------------

def build(args):
    texts, balls, (x0, x1, z0, z1) = layout(args.layout)
    mats = list(MATERIALS)
    mid = {m: i + 1 for i, m in enumerate(mats)}   # 0 is the floor

    # Camera: frame the bounding box of the scene, looking a little down at it.
    aspect = args.width / args.height
    fov = math.radians(args.fov)
    th = math.tan(fov / 2)
    w, h = (x1 - x0) * 1.10, (z1 - z0) * 1.18
    dist = max(h / 2 / th, w / 2 / (th * aspect))
    target = ((x0 + x1) / 2, 0.0, (z0 + z1) / 2 * 0.92)
    elev = math.radians(args.elevation)
    eye = (target[0], -dist * math.cos(elev), target[2] + dist * math.sin(elev))
    fwd = norm(sub(target, eye))
    right = norm(cross(fwd, (0.0, 0.0, 1.0)))
    up = cross(right, fwd)
    span = max(x1 - x0, z1 - z0)
    light = (x0 + 0.2 * (x1 - x0), -0.9 * dist, z1 + 1.6 * span)

    const("eps", "0.0005")
    const("eye", vec(eye))
    const("fwd", vec(fwd))
    const("right", vec(vm_(right, th * aspect)))
    const("up", vec(vm_(up, th)))
    const("light", vec(light))
    const("lightR", num(0.03 * span))
    const("amb", num(THEME["amb"]))
    const("shin", "70.0")
    const("horizon", vec(srgb(THEME["horizon"])))
    const("zenith", vec(srgb(THEME["zenith"])))
    const("haze", vec(srgb(THEME["haze"])))
    const("fogStart", num(dist * 0.9))
    const("fogRange", num(dist * 2.5))
    const("exposure", "1.05")

    define_vectors()
    macro("sphT", ["p", "d", "rr"], let(
        [("b", "vd(p, d)"), ("disc", "b * b - (vd(p, p) - rr)")],
        "if(disc > 0.0, if(-b - sqrt(greatest(disc, 0.0)) > $eps, -b - sqrt(greatest(disc, 0.0)), 1e9), 1e9)"))
    macro("planeT", ["o", "d"], f"if({el('d', 3)} < -0.000001 AND {el('o', 3)} > 0.0, -{el('o', 3)} / least({el('d', 3)}, -0.000001), 1e9)")
    macro("sky", ["d"], f"va(vm($horizon, 1.0 - least(greatest({el('d', 3)} * 3.0, 0.0), 1.0)), vm($zenith, least(greatest({el('d', 3)} * 3.0, 0.0), 1.0)))")
    macro("checker", ["p"], f"if(({floori(el('p', 1) + ' / ' + num(FLOOR['tile']))} + {floori(el('p', 2) + ' / ' + num(FLOOR['tile']))}) % 2 = 0, {vec(srgb(THEME['floor_a']))}, {vec(srgb(THEME['floor_b']))})")
    macro("rnd", ["a", "b", "c"], ("(cityHash64(a, b, c) % 1048576) / 1048576.0" if CH
                                   else "(hash_city64(a, b, c) % 1048576) / 1048576.0"))

    # A text block: intersect the ray with the slab |y - y0| <= r that holds the
    # spheres, and test only the grid cells under the part of the ray inside it.
    for k, t in enumerate(texts):
        const(f"bits{k}", t.bits())
        macro(f"cen{k}", ["i"], f"({num(t.x0)} + (i % {t.nc}) * {num(t.sp)}, {num(t.y0)}, {num(t.zt)} - {idiv('i', t.nc)} * {num(t.sp)})")
        ox, oy, oz = el("o", 1), el("o", 2), el("o", 3)
        dx, dy, dz = el("d", 1), el("d", 2), el("d", 3)
        y0, r, sp = num(t.y0), num(t.r), num(t.sp)
        ts = amap("c", "cands", f"if($bits{k}[c + 1] = 1, sphT(vs(o, cen{k}(c)), d, {num(t.r * t.r)}), 1e9)")
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
            ("gx0", f"greatest({floori(f'(least(xa, xb) - {r} - {num(t.x0)}) / {sp}')}, 0)"),
            ("gx1", f"least({floori(f'(greatest(xa, xb) + {r} - {num(t.x0)}) / {sp}')} + 1, {t.nc - 1})"),
            ("gy0", f"greatest({floori(f'({num(t.zt)} - greatest(za, zb) - {r}) / {sp}')}, 0)"),
            ("gy1", f"if(t1 < t0, -1, least({floori(f'({num(t.zt)} - least(za, zb) + {r}) / {sp}')} + 1, {t.nr - 1}))"),
            ("cands", flatten(amap("gy", "range(gy0, gy1 + 1)", amap("gx", "range(gx0, gx1 + 1)", f"gy * {t.nc} + gx")))),
            ("ts", ts),
            ("tm", amin(push("ts", "1e9"))),
        ], f"(tm, if(tm < 1e8, cands[{index_of('ts', 'tm')}], 0))"))

    def ball_t(b, o):
        return f"sphT(vs({o}, {vec(b.c)}), d, {num(b.r * b.r)})"

    hits = [f"{el(f'text{k}(o, d)', 1)} < maxd" for k in range(len(texts))]
    hits += [f"{ball_t(b, 'o')} < maxd" for b in balls]
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
        objs.append((f"t{k}", mid[texts[k].mat], f"vn(vs(hp, cen{k}({el(f'h{k}', 2)})))"))
    for j, b in enumerate(balls):
        binds.append((f"l{j}", ball_t(b, "o")))
        objs.append((f"l{j}", mid[b.mat], f"vn(vs(hp, {vec(b.c)}))"))
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
        ("sh", "if(miss, 0.0, if(occluded(shO, ldir, distL), 0.0, 1.0))"),
        ("lam", "greatest(vd(ldir, nrm), 0.0) * sh"),
        ("spec", "pow(greatest(vd(ldir, rdir), 0.0), $shin) * sh * matSpec(m)"),
        ("kr", "matKr(m)"),
        ("base", "if(isFloor, checker(hp), matCol(m))"),
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
    chan = lambda i: (f"CAST(round(least(greatest(pow(greatest(avg({el('col', i)}), 0.0) * $exposure, 1.0 / 2.2), 0.0), 1.0) * 255.0) AS INTEGER)")
    st0 = (f"(st0r)" if CH else "st0r")
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
        sql = "WITH\n    " + ",\n    ".join(withs) + "\n" + query
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
    p.add_argument("--layout", choices=["investors", "banner"], default="investors")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--samples", type=int, default=4)
    p.add_argument("--depth", type=int, default=3)
    p.add_argument("--theme", choices=list(THEMES), default="dark")
    p.add_argument("--fov", type=float, default=30.0)
    p.add_argument("--elevation", type=float, default=8.0, help="camera pitch, degrees")
    args = p.parse_args()
    global CH, THEME
    CH = args.dialect == "clickhouse"
    THEME = THEMES[args.theme]
    sys.stdout.write(build(args))


if __name__ == "__main__":
    main()
