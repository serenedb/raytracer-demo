"""Stroke font: every glyph is a few polylines, rendered as tubes (chains of capsules).

Units: capitals are 6 high, lowercase 4; y is up and the baseline is y = 0.
"""
import math

CAP = 6.0
SPACING = 1.7   # between the boxes of two glyphs
SPACE = 2.4     # the width of ' '


def arc(cx, cy, rx, ry, a0, a1):
    """Points on an ellipse from angle a0 to a1 (degrees, either direction)."""
    n = max(2, math.ceil(abs(a1 - a0) / 12))
    return [(cx + rx * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
             cy + ry * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]


O = [arc(2.1, 3, 2.1, 3, 0, 360)]

GLYPHS = {
    'A': [[(0, 0), (2, 6), (4, 0)], [(0.75, 2.2), (3.25, 2.2)]],
    'B': [[(2.4, 3.1), (0, 3.1)], [(2.4, 6), (0, 6), (0, 0), (2.5, 0)] + arc(2.5, 1.55, 1.55, 1.55, -90, 90) + [(2.4, 3.1)],
          arc(2.4, 4.55, 1.45, 1.45, -90, 90)],
    'C': [arc(2.1, 3, 2.1, 3, 40, 320)],
    'D': [[(1.5, 0), (0, 0), (0, 6), (1.5, 6)] + arc(1.5, 3, 2.5, 3, 90, -90)],
    'E': [[(3.8, 6), (0, 6), (0, 0), (3.8, 0)], [(0, 3.1), (3.2, 3.1)]],
    'F': [[(3.8, 6), (0, 6), (0, 0)], [(0, 3.1), (3.2, 3.1)]],
    'G': [arc(2.1, 3, 2.1, 3, 45, 360) + [(2.5, 3)]],
    'H': [[(0, 0), (0, 6)], [(4, 0), (4, 6)], [(0, 3.1), (4, 3.1)]],
    'I': [[(0, 0), (0, 6)]],
    'J': [[(3.6, 6), (3.6, 1.8)] + arc(1.8, 1.8, 1.8, 1.8, 0, -180)],
    'K': [[(0, 0), (0, 6)], [(3.8, 6), (0, 2.2)], [(1.25, 3.45), (4, 0)]],
    'L': [[(0, 6), (0, 0), (3.6, 0)]],
    'M': [[(0, 0), (0, 6), (2.5, 2), (5, 6), (5, 0)]],
    'N': [[(0, 0), (0, 6), (4, 0), (4, 6)]],
    'O': O,
    'P': [[(0, 0), (0, 6), (2.3, 6)] + arc(2.3, 4.4, 1.6, 1.6, 90, -90) + [(0, 2.8)]],
    'Q': O + [[(2.7, 1.3), (4.4, -0.5)]],
    'R': [[(0, 0), (0, 6), (2.3, 6)] + arc(2.3, 4.4, 1.6, 1.6, 90, -90) + [(0, 2.8)], [(2.0, 2.8), (4, 0)]],
    'S': [arc(2, 4.5, 1.9, 1.5, 20, 270) + arc(2, 1.5, 2.0, 1.5, 90, -160)[1:]],
    'T': [[(0, 6), (4.4, 6)], [(2.2, 6), (2.2, 0)]],
    'U': [[(0, 6), (0, 2)] + arc(2, 2, 2, 2, 180, 360) + [(4, 6)]],
    'V': [[(0, 6), (2, 0), (4, 6)]],
    'W': [[(0, 6), (1.25, 0), (2.5, 4.2), (3.75, 0), (5, 6)]],
    'X': [[(0, 0), (4, 6)], [(0, 6), (4, 0)]],
    'Y': [[(0, 6), (2, 3), (4, 6)], [(2, 3), (2, 0)]],
    'Z': [[(0, 6), (4, 6), (0, 0), (4, 0)]],
    '0': [arc(1.8, 3, 1.8, 3, 0, 360)],
    '1': [[(0, 4.8), (1.3, 6), (1.3, 0)]],
    '6': [arc(2, 1.9, 2, 1.9, 0, 360), [(0.1, 2.4), (2.6, 6)]],
    '2': [arc(2, 4.1, 2, 1.9, 160, -35) + [(0, 0), (4.2, 0)]],
    'e': [[(0, 2), (4, 2)] + arc(2, 2, 2, 2, 0, 320)[1:]],
    'n': [[(0, 0), (0, 4)], arc(2, 2, 2, 2, 180, 0) + [(4, 0)]],
    'r': [[(0, 0), (0, 4)], arc(2.2, 1.8, 2.2, 2.2, 180, 60)],
    '-': [[(0.3, 3), (3.3, 3)]],
    '.': [[(0.3, 0), (0.3, 0.01)]],
    ',': [[(0.6, 0.4), (0.1, -0.75)]],
}


def glyph(ch):
    if ch not in GLYPHS and ch.upper() in GLYPHS:
        ch = ch.upper()
    if ch not in GLYPHS:
        raise SystemExit(f"font.py has no glyph for {ch!r}")
    return GLYPHS[ch]


def layout(text, spacing=SPACING, stretch=1.0):
    """Segments ((x0, y0), (x1, y1)) of `text` starting at x = 0, and its width.

    `stretch` widens every glyph; `spacing` is the gap between two glyph boxes."""
    segs, x = [], 0.0
    for i, ch in enumerate(text):
        if ch == ' ':
            x += SPACE * stretch
            continue
        lines = glyph(ch)
        w = max(px for line in lines for px, _ in line) * stretch
        segs += [((x + a[0] * stretch, a[1]), (x + b[0] * stretch, b[1])) for line in lines for a, b in zip(line, line[1:])]
        x += w + (spacing if i + 1 < len(text) else 0)
    return segs, x
