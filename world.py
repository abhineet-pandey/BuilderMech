"""Static map model: a 1 m height grid of everything solid, plus line-of-sight and ground queries.

H[j, i] = height of the highest solid surface you would stand on at (x, y): terrain, or the top of a
building/stadium prism. Prisms whose base is well above the ground (bridges, overhangs you can walk
under) are kept out of H so they don't look like walls.
"""
import json
import math
import os
import sys
import time

import numpy as np


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


def _poly_mask(ring, xs, ys):
    """Even-odd point-in-polygon for the grid points xs (1D, columns) x ys (1D, rows)."""
    px = xs[None, :]
    py = ys[:, None]
    inside = np.zeros((ys.size, xs.size), dtype=bool)
    n = len(ring) // 2
    for k in range(n):
        x1, y1 = ring[2 * k], ring[2 * k + 1]
        x2, y2 = ring[2 * ((k + 1) % n)], ring[2 * ((k + 1) % n) + 1]
        if y1 == y2:
            continue
        cond = (y1 > py) != (y2 > py)
        xint = (x2 - x1) * (py - y1) / (y2 - y1) + x1
        inside ^= cond & (px < xint)
    return inside


class World:
    def __init__(self, map_dir):
        t0 = time.time()
        wpath = os.path.join(map_dir, "v2", "world.json")
        with open(wpath) as f:
            w = json.load(f)
        ter = w["terrain"]
        self.minX = float(ter["minX"])
        self.minY = float(ter["minY"])
        self.step = float(ter["step"])
        self.nx = int(ter["nx"])
        self.ny = int(ter["ny"])
        scale = float(ter.get("scale", 0.01))
        raw = np.fromfile(os.path.join(map_dir, "v2", ter.get("file", "terrain.i16")), dtype="<i2")
        self.G = (raw.reshape(self.ny, self.nx).astype(np.float32)) * scale
        H = self.G.copy()
        xs_all = self.minX + np.arange(self.nx) * self.step
        ys_all = self.minY + np.arange(self.ny) * self.step
        self.overheads = []
        for p in w["prisms"]:
            ring = p["ring"]
            rx = ring[0::2]
            ry = ring[1::2]
            i0 = max(0, int(math.floor((min(rx) - self.minX) / self.step)))
            i1 = min(self.nx - 1, int(math.ceil((max(rx) - self.minX) / self.step)))
            j0 = max(0, int(math.floor((min(ry) - self.minY) / self.step)))
            j1 = min(self.ny - 1, int(math.ceil((max(ry) - self.minY) / self.step)))
            if i1 < i0 or j1 < j0:
                continue
            xs = xs_all[i0:i1 + 1]
            ys = ys_all[j0:j1 + 1]
            m = _poly_mask(ring, xs, ys)
            for hole in p.get("holes") or []:
                m &= ~_poly_mask(hole, xs, ys)
            if not m.any():
                continue
            sub = H[j0:j1 + 1, i0:i1 + 1]
            gmed = float(np.median(self.G[j0:j1 + 1, i0:i1 + 1][m]))
            top = float(p["top"])
            if float(p["base"]) > gmed + 6.0:
                self.overheads.append(p)
                continue
            np.maximum(sub, np.where(m, top, -1e9).astype(np.float32), out=sub)
        self.H = H
        self.bounds = w["bounds"]
        _log(f"world loaded in {time.time() - t0:.2f}s, {len(self.overheads)} overheads")

    # --- queries -------------------------------------------------------------------------
    def _ij(self, x, y):
        i = int(round((x - self.minX) / self.step))
        j = int(round((y - self.minY) / self.step))
        if i < 0:
            i = 0
        elif i >= self.nx:
            i = self.nx - 1
        if j < 0:
            j = 0
        elif j >= self.ny:
            j = self.ny - 1
        return i, j

    def height(self, x, y):
        """Top solid surface at (x, y)."""
        i, j = self._ij(x, y)
        return float(self.H[j, i])

    def ground(self, x, y):
        i, j = self._ij(x, y)
        return float(self.G[j, i])

    def heights_along(self, x0, y0, x1, y1, n):
        """H sampled at n points from (x0,y0) to (x1,y1) inclusive."""
        t = np.linspace(0.0, 1.0, n)
        xs = x0 + (x1 - x0) * t
        ys = y0 + (y1 - y0) * t
        ii = np.clip(np.rint((xs - self.minX) / self.step).astype(np.int32), 0, self.nx - 1)
        jj = np.clip(np.rint((ys - self.minY) / self.step).astype(np.int32), 0, self.ny - 1)
        return self.H[jj, ii]

    def sample(self, X, Y):
        """H at arrays of points (nearest cell)."""
        ii = np.clip(np.rint((X - self.minX) / self.step).astype(np.int32), 0, self.nx - 1)
        jj = np.clip(np.rint((Y - self.minY) / self.step).astype(np.int32), 0, self.ny - 1)
        return self.H[jj, ii]

    def los(self, a, b, margin=0.2):
        """True if the straight segment a->b (3D) clears every building and the terrain."""
        dx = b[0] - a[0]
        dy = b[1] - a[1]
        d = math.hypot(dx, dy)
        if d < 2.0:
            return True
        n = int(d / 1.0) + 2
        t = np.linspace(0.0, 1.0, n)
        # skip the very ends (the shooter's and the target's own cells)
        keep = (t * d > 1.5) & ((1.0 - t) * d > 1.5)
        t = t[keep]
        if t.size == 0:
            return True
        xs = a[0] + dx * t
        ys = a[1] + dy * t
        zs = a[2] + (b[2] - a[2]) * t
        ii = np.clip(np.rint((xs - self.minX) / self.step).astype(np.int32), 0, self.nx - 1)
        jj = np.clip(np.rint((ys - self.minY) / self.step).astype(np.int32), 0, self.ny - 1)
        return bool(np.all(zs > self.H[jj, ii] + margin))

    def los_many(self, a, pts, margin=0.2):
        """Vectorised LOS from a to each point in pts (list of 3D). Returns list of bools."""
        if not pts:
            return []
        P = np.asarray(pts, dtype=np.float64)
        d = np.hypot(P[:, 0] - a[0], P[:, 1] - a[1])
        n = 64
        t = np.linspace(0.0, 1.0, n)[None, :]
        xs = a[0] + (P[:, 0:1] - a[0]) * t
        ys = a[1] + (P[:, 1:2] - a[1]) * t
        zs = a[2] + (P[:, 2:3] - a[2]) * t
        dd = d[:, None] * t
        ends = (dd > 1.5) & ((d[:, None] - dd) > 1.5)
        ii = np.clip(np.rint((xs - self.minX) / self.step).astype(np.int32), 0, self.nx - 1)
        jj = np.clip(np.rint((ys - self.minY) / self.step).astype(np.int32), 0, self.ny - 1)
        ok = (zs > self.H[jj, ii] + margin) | ~ends
        return list(np.all(ok, axis=1))

    def in_bounds(self, x, y, margin=3.0):
        b = self.bounds
        return b["minX"] + margin < x < b["maxX"] - margin and b["minY"] + margin < y < b["maxY"] - margin
