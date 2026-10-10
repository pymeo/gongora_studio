"""Teaser vertical de "A motor parado" (GONGORA).

Render 100% procedural y local: numpy + Pillow + ffmpeg (imageio-ffmpeg).
No usa servicios externos ni publica nada.

Uso:
    ops/video/.venv/bin/python ops/video/render_teaser.py            # render completo
    ops/video/.venv/bin/python ops/video/render_teaser.py --stills 1 5 9.5   # fotogramas sueltos
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from multiprocessing import Pool
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FF = imageio_ffmpeg.get_ffmpeg_exe()
OUTDIR = Path("output/social/a_motor_parado")
WORK = OUTDIR / "work"
W, H = 1080, 1920
LW, LH = 540, 960  # fondo desenfocado a media resolucion
FPS = 30
DUR = 24.0
NFRAMES = int(DUR * FPS)
FONT = "/usr/share/fonts/truetype/ubuntu/UbuntuSans[wdth,wght].ttf"

_an = json.load(open(WORK / "analysis.json"))
PERIOD = _an["period"]
PHASE = _an["phase"]
BAR = 4 * PERIOD


def bar(k: float) -> float:
    return PHASE + k * BAR


def beat(k: float) -> float:
    return PHASE + k * PERIOD


# --------------------------------------------------------------------------- utilidades
def smooth(a, b, x):
    t = np.clip((x - a) / (b - a), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def sm(a, b, x):  # escalar
    return float(smooth(a, b, x))


def _box(a, r, axis):
    if r < 1:
        return a
    a = np.moveaxis(a, axis, 0)
    n = a.shape[0]
    pad = np.concatenate([np.repeat(a[:1], r, 0), a, np.repeat(a[-1:], r + 1, 0)])
    c = np.cumsum(pad, 0, dtype=np.float32)
    c = np.concatenate([np.zeros_like(c[:1]), c])
    out = (c[2 * r + 1:2 * r + 1 + n] - c[:n]) / (2 * r + 1)
    return np.moveaxis(out, 0, axis)


def blur(a, sigma):
    if sigma < 0.5:
        return a
    r = max(1, int(round((math.sqrt(4 * sigma * sigma / 3 + 1) - 1) / 2)))
    for _ in range(3):
        a = _box(a, r, 0)
        a = _box(a, r, 1)
    return a


def capsule(cv, x0, y0, x1, y1, r, col, inten=1.0, soft=1.0, ring=0.0, stretch_y=1.0):
    """Disco (o estela si p0 != p1) aditivo con borde suave. Bokeh con anillo opcional."""
    h, w, _ = cv.shape
    pad = r * stretch_y + 2
    xmin = int(max(0, min(x0, x1) - r - 2)); xmax = int(min(w, max(x0, x1) + r + 3))
    ymin = int(max(0, min(y0, y1) - pad)); ymax = int(min(h, max(y0, y1) + pad + 1))
    if xmin >= xmax or ymin >= ymax or r <= 0:
        return
    ys, xs = np.mgrid[ymin:ymax, xmin:xmax].astype(np.float32)
    dx, dy = x1 - x0, y1 - y0
    l2 = dx * dx + dy * dy
    if l2 < 1e-6:
        px, py = xs - x0, (ys - y0) / stretch_y
    else:
        t = np.clip(((xs - x0) * dx + (ys - y0) * dy) / l2, 0, 1)
        px, py = xs - x0 - t * dx, (ys - y0 - t * dy) / stretch_y
    d = np.sqrt(px * px + py * py)
    a = np.clip((r - d) / soft + 0.5, 0, 1)
    if ring:
        a = a * (1 - ring + ring * 1.7 * np.clip(d / r, 0, 1) ** 4)
    L = math.sqrt(l2)
    scale = inten * (2 * r) / (2 * r + L)
    cv[ymin:ymax, xmin:xmax] += a[..., None] * (np.asarray(col, np.float32) * scale)


def vgrad(top, bottom, h=LH, w=LW, curve=1.0):
    y = (np.linspace(0, 1, h, dtype=np.float32) ** curve)[:, None, None]
    return np.broadcast_to(np.asarray(top, np.float32) * (1 - y) + np.asarray(bottom, np.float32) * y, (h, w, 3)).copy()


def hnoise(t, seed, amp=1.0, speed=0.6):
    """Ruido suave 1D (movimiento de camara en mano)."""
    v = 0.0
    for k, f in enumerate((1.0, 2.13, 4.7)):
        v += math.sin(t * speed * f * 2 * math.pi + seed * (k + 1) * 1.7) / (k + 1)
    return v * amp


# --------------------------------------------------------------------------- paleta
AMBER = (1.0, 0.58, 0.22)
SODIUM = (1.0, 0.45, 0.12)
WHITE = (0.85, 0.9, 1.0)
WARMW = (1.0, 0.85, 0.65)
RED = (1.0, 0.12, 0.08)
TEAL = (0.15, 0.85, 0.8)
MAGENTA = (0.95, 0.25, 0.6)
BLUE = (0.3, 0.5, 1.0)
GREEN = (0.15, 1.0, 0.55)


# --------------------------------------------------------------------------- ciudad (modelo tunel)
class City:
    def __init__(self, seed=7, n=260):
        rng = np.random.default_rng(seed)
        side = rng.choice([-1, 1], n)
        kind = rng.choice(4, n, p=[0.45, 0.25, 0.18, 0.12])  # ventanas, farolas, coches, neones
        X = np.where(kind == 1, side * rng.uniform(1.6, 2.2, n),
                     np.where(kind == 2, rng.uniform(-1.2, 1.2, n), side * rng.uniform(1.8, 7.5, n)))
        Y = np.where(kind == 1, -1.6 + rng.normal(0, 0.05, n),
                     np.where(kind == 2, 0.55 + rng.normal(0, 0.03, n), rng.uniform(-6.5, 0.3, n)))
        self.X, self.Y = X.astype(np.float32), Y.astype(np.float32)
        self.Z0 = rng.uniform(0, 40, n).astype(np.float32)
        pal_win = [WARMW, AMBER, WHITE, TEAL]
        pal_neon = [MAGENTA, TEAL, BLUE, AMBER]
        cols = []
        for i in range(n):
            if kind[i] == 0:
                cols.append(pal_win[rng.integers(4)])
            elif kind[i] == 1:
                cols.append(SODIUM)
            elif kind[i] == 2:
                cols.append(RED if X[i] < 0.3 else WARMW)
            else:
                cols.append(pal_neon[rng.integers(4)])
        self.col = np.array(cols, np.float32)
        self.kind = kind
        self.size = np.where(kind == 1, 1.6, np.where(kind == 3, 1.3, 1.0)).astype(np.float32) * rng.uniform(0.7, 1.3, n)
        self.bright = rng.uniform(0.5, 1.0, n).astype(np.float32)

    def render(self, cv, s, v, cx, cy, f=330.0, shutter=0.04, defocus=1.0, gain=1.0, roll=0.0,
               reflect=True, palette_shift=None):
        near, zmax = 0.6, 40.0
        Z = near + np.mod(self.Z0 - s, zmax - near)
        cr, sr = math.cos(roll), math.sin(roll)
        order = np.argsort(-Z)
        for i in order:
            z = Z[i]
            X, Y = self.X[i], self.Y[i]
            x0p, y0p = f * X / z, f * Y / z
            z1 = z + max(0.0, v * shutter)
            x1p, y1p = f * X / z1, f * Y / z1
            rot = lambda x, y: (cx + x * cr - y * sr, cy + x * sr + y * cr)
            ax, ay = rot(x0p, y0p)
            bx, by = rot(x1p, y1p)
            if max(ax, bx) < -60 or min(ax, bx) > LW + 60 or max(ay, by) < -60 or min(ay, by) > LH + 60:
                continue
            r = self.size[i] * (2.0 + defocus * 26.0 / (z + 1.5))
            fade = min(1.0, (zmax - z) / 22.0) * min(1.0, z / 1.2)
            inten = gain * self.bright[i] * fade * (2.4 / (r ** 0.55))
            col = self.col[i] if palette_shift is None else palette_shift(self.col[i])
            capsule(cv, ax, ay, bx, by, r, col, inten, soft=1.2, ring=0.25 if r > 6 else 0.0)
            if reflect and self.kind[i] != 2 and Y < 0.2:
                ry = cy + (2 * 0.9 - Y) * f / z
                if ry < LH + 40:
                    capsule(cv, ax, ry, bx, cy + (2 * 0.9 - Y) * f / z1, r * 0.8, col, inten * 0.25,
                            soft=2.0, stretch_y=3.5)


CITY = City(7)
CITY2 = City(21, 200)


def distance_curve(t):
    """Distancia recorrida. Rapido al principio, frena y se detiene en bar(2)."""
    t_stop = bar(2)
    v0 = 22.0
    if t < bar(1):
        return v0 * t, v0
    if t < t_stop:
        T = t_stop - bar(1)
        u = (t - bar(1)) / T
        # deceleracion suave: v = v0 * (1-u)^2
        s = v0 * bar(1) + v0 * T * (1 - (1 - u) ** 3) / 3
        return s, v0 * (1 - u) ** 2
    return v0 * bar(1) + v0 * (t_stop - bar(1)) / 3, 0.0


S_STOP = distance_curve(bar(2) + 1)[0]


# --------------------------------------------------------------------------- lluvia
class Rain:
    def __init__(self, seed=3, n=650):
        rng = np.random.default_rng(seed)
        self.x = rng.uniform(0, W, n)
        self.y = rng.uniform(0, H, n)
        self.r = (rng.pareto(2.2, n) * 6 + 5.0).clip(5, 30)
        self.born = rng.uniform(0, 1, n)  # umbral de densidad
        self.slide = rng.uniform(0, 1, n) < 0.07
        self.speed = rng.uniform(120, 420, n)
        self.phase = rng.uniform(0, 30, n)

    def apply(self, img, t, density, slide=1.0, cleared=None, mag=2.6, macro=1.0):
        """img: float HxWx3 a resolucion completa. Cada gota muestra la escena invertida (lente)."""
        out = img
        for i in range(len(self.x)):
            if self.born[i] > density:
                continue
            r = self.r[i] * macro
            cx, cy = self.x[i], self.y[i]
            if self.slide[i] and slide > 0:
                cy = (cy + ((t + self.phase[i]) * self.speed[i] * slide)) % (H + 80) - 40
                r = r * 1.1
            if cleared is not None and cleared(cx, cy):
                continue
            ri = int(r) + 2
            x0, x1 = int(max(0, cx - ri)), int(min(W, cx + ri + 1))
            y0, y1 = int(max(0, cy - ri)), int(min(H, cy + ri + 1))
            if x0 >= x1 or y0 >= y1:
                continue
            ys, xs = np.mgrid[y0:y1, x0:x1].astype(np.float32)
            px, py = (xs - cx) / r, (ys - cy) / (r * 1.08)
            d = np.sqrt(px * px + py * py)
            a = np.clip((1 - d) * r * 0.9, 0, 1)
            if not a.any():
                continue
            sx = np.clip(cx - px * r * mag, 0, W - 1).astype(np.int32)
            sy = np.clip(cy - py * r * mag - r * 2, 0, H - 1).astype(np.int32)
            lens = img[sy, sx] * 1.35 + 0.012
            shade = 1 - 0.8 * smooth(0.5, 1.0, d) * (0.45 + 0.55 * np.clip(py + 0.3, 0, 1))
            lens = lens * shade[..., None]
            hl = np.exp(-(((px + 0.3) ** 2 + (py + 0.36) ** 2) / 0.014)) * 0.7 + smooth(0.75, 0.97, d) * np.clip(py, 0, 1) * 0.12
            lens = lens + hl[..., None] * np.array([1.0, 0.97, 0.92], np.float32)
            reg = out[y0:y1, x0:x1]
            out[y0:y1, x0:x1] = reg * (1 - a[..., None]) + lens * a[..., None]
            if self.slide[i] and slide > 0:
                # rastro humedo encima de la gota
                tx0, tx1 = int(max(0, cx - r * 0.35)), int(min(W, cx + r * 0.35 + 1))
                ty0, ty1 = int(max(0, cy - r * 14)), int(max(0, cy - r))
                if tx0 < tx1 and ty0 < ty1:
                    g = np.linspace(0, 1, ty1 - ty0, dtype=np.float32)[:, None, None]
                    out[ty0:ty1, tx0:tx1] = out[ty0:ty1, tx0:tx1] * (1 + 0.25 * g)
        return out


RAIN = Rain()


# --------------------------------------------------------------------------- piezas
def sky(t, kind="night"):
    if kind == "night":
        return vgrad((0.010, 0.014, 0.035), (0.05, 0.03, 0.07), curve=0.8)
    if kind == "dawn":
        return vgrad((0.05, 0.07, 0.16), (0.30, 0.17, 0.20), curve=1.3)
    raise ValueError(kind)


def traffic_light(cv, color, inten, x=LW * 0.5, y=LH * 0.215, r=44):
    capsule(cv, x, y, x, y, r, color, inten, soft=2.0, ring=0.35)
    capsule(cv, x, y, x, y, r * 2.6, color, inten * 0.10, soft=30)
    # reflejo en el asfalto mojado
    capsule(cv, x, LH * 0.86, x, LH * 0.86, r * 0.45, color, inten * 0.10, soft=8, stretch_y=3.0)


def car_ahead(cv, brake, y=LH * 0.64):
    k = 0.55 + 1.3 * brake
    for x in (LW * 0.33, LW * 0.67):
        capsule(cv, x - 14, y, x + 14, y, 22, RED, k * 0.9, soft=2.0, ring=0.2)
    capsule(cv, LW * 0.5 - 30, y - 70, LW * 0.5 + 30, y - 70, 9, RED, k * 0.6 * brake, soft=2)
    capsule(cv, LW * 0.5, y + 60, LW * 0.5, y + 60, 26, (0.9, 0.85, 0.7), 0.05, soft=6, stretch_y=0.4)


def cross_traffic(cv, t, times, y=LH * 0.47, dur=0.55):
    """Coches cruzando delante: uno por cada instante de `times` (sincronizado a pulsos)."""
    for k, tc in enumerate(times):
        u = (t - tc) / dur
        if -0.6 < u < 0.6:
            d = 1 if k % 2 == 0 else -1
            x = LW * 0.5 + d * u * LW * 1.8
            col_front, col_back = (WARMW, RED) if d > 0 else (RED, WARMW)
            yy = y + (k % 3) * 10
            for off, col, inten in ((40 * d, col_front, 1.6), (-120 * d, col_back, 1.0)):
                xx = x + off
                capsule(cv, xx - 70 * d, yy, xx, yy, 9, col, inten, soft=2)
                capsule(cv, xx - 70 * d, yy + 30, xx, yy + 30, 6, col, inten * 0.25, soft=6, stretch_y=3)


def glow(cv, k1=0.5, k2=0.35):
    return cv + blur(cv, 5) * k1 + blur(cv, 22) * k2


def tonemap(x, exposure=1.0):
    y = 1 - np.exp(-x * exposure * 1.6)
    # grade: sombras hacia azul petroleo, altas hacia ambar
    lum = (y * np.array([0.3, 0.55, 0.15], np.float32)).sum(-1, keepdims=True)
    shadow = (1 - lum) ** 3
    y = y + shadow * np.array([0.0, 0.018, 0.03], np.float32)
    y = y * (1 + lum * np.array([0.04, 0.0, -0.05], np.float32))
    return np.clip(y, 0, 1)


def up(lo):
    """Escala el fondo de media resolucion a 1080x1920 (bilineal por numpy, mantiene HDR)."""
    a = lo.repeat(2, 0).repeat(2, 1)
    return blur(a, 0.8)


# --------------------------------------------------------------------------- volante y manos
_WHEEL = None


def wheel_mask():
    """Devuelve (mascara volante+salpicadero, mascara manos) a 1080x1920."""
    global _WHEEL
    if _WHEEL is not None:
        return _WHEEL
    s = 2
    im = Image.new("L", (W * s, H * s), 0)
    hm = Image.new("L", (W * s, H * s), 0)
    d, dh = ImageDraw.Draw(im), ImageDraw.Draw(hm)
    cx, cy, rx, ry, th = 540 * s, 1900 * s, 640 * s, 520 * s, 72 * s
    d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=255)
    d.ellipse([cx - rx + th, cy - ry + th, cx + rx - th, cy + ry - th], fill=0)
    d.ellipse([cx - 200 * s, cy - 170 * s, cx + 200 * s, cy + 210 * s], fill=255)
    for side in (-1, 1):
        d.line([(cx, cy), (cx + side * (rx - th / 2), cy + 40 * s)], fill=255, width=int(70 * s))
    d.ellipse([-700 * s, 1840 * s, W * s + 700 * s, 2700 * s], fill=255)
    # manos a las 10 y a las 2
    for side in (-1, 1):
        ang = math.radians(-90 + side * 38)
        hx = cx + (rx - th / 2) * math.cos(ang)
        hy = cy + (ry - th / 2) * math.sin(ang)
        ex, ey = cx + side * 700 * s, 2150 * s
        dh.line([(hx + side * 10 * s, hy + 60 * s), (ex, ey)], fill=255, width=int(118 * s))
        dh.ellipse([hx - 68 * s, hy - 46 * s, hx + 68 * s, hy + 92 * s], fill=255)
        for j in range(4):
            fa = ang - side * math.radians(-6.5 + j * 4.6)
            fx = cx + (rx - th * 0.05) * math.cos(fa)
            fy = cy + (ry - th * 0.05) * math.sin(fa)
            dh.ellipse([fx - 21 * s, fy - 24 * s, fx + 21 * s, fy + 24 * s], fill=255)
        tx, ty = hx - side * 66 * s, hy + 6 * s
        dh.line([(hx - side * 10 * s, hy + 34 * s), (tx, ty)], fill=255, width=int(34 * s))
        dh.ellipse([tx - 17 * s, ty - 17 * s, tx + 17 * s, ty + 17 * s], fill=255)
    out = []
    for x in (im, hm):
        x = x.resize((W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(2.5))
        out.append(np.asarray(x, np.float32) / 255)
    _WHEEL = tuple(out)
    return _WHEEL


def apply_wheel(img, light_col, sweep_x=None, sweep_k=0.0):
    mw, mh = wheel_mask()
    m = np.maximum(mw, mh)
    light = np.asarray(light_col, np.float32)[None, None, :] * np.ones((1, W, 1), np.float32)
    if sweep_x is not None:
        xs = np.arange(W, dtype=np.float32)[None, :, None]
        light = light + np.exp(-((xs - sweep_x) / 200) ** 2) * sweep_k * np.array([1.0, 0.9, 0.75], np.float32)
    # luz cenital: mas luz cuanto mas arriba del borde (mascara desplazada)
    top = np.clip(m - np.roll(m, 9, axis=0), 0, 1)[..., None]
    top = blur(top, 2.0)
    soft = blur(mh[::4, ::4, None], 6).repeat(4, 0).repeat(4, 1)
    skin = np.array([0.62, 0.40, 0.33], np.float32)
    body = np.array([0.010, 0.010, 0.016], np.float32)
    hands = skin * light * (0.07 + 0.10 * soft)
    base = body * mw[..., None] + 0.0
    ys = np.arange(H, dtype=np.float32)[:, None, None]
    img = img * (1 - 0.75 * smooth(1420, 1820, ys))  # salpicadero en sombra
    out = img * (1 - m[..., None]) + (base + hands * mh[..., None]) * (m[..., None] > 0)
    return out + top * light * 0.6

# --------------------------------------------------------------------------- texto
def _font(size, wght, wdth=100):
    f = ImageFont.truetype(FONT, size)
    f.set_variation_by_axes([wdth, wght])
    return f


def text_patch(text, size, wght, track_em, max_w=780, color=(255, 255, 255)):
    while True:
        f = _font(size, wght)
        tr = track_em * size
        widths = [f.getlength(c) for c in text]
        tw = sum(widths) + tr * (len(text) - 1)
        if tw <= max_w or size < 12:
            break
        size -= 2
    asc, desc = f.getmetrics()
    pad = 40
    im = Image.new("L", (int(tw) + 2 * pad, asc + desc + 2 * pad), 0)
    d = ImageDraw.Draw(im)
    x = pad
    for c, w in zip(text, widths):
        d.text((x, pad), c, font=f, fill=255)
        x += w + tr
    a = np.asarray(im, np.float32) / 255
    sh = blur(a[..., None], 10)[..., 0]
    return {"a": a, "sh": sh, "col": np.array(color, np.float32) / 255, "w": im.width, "h": im.height}


def put_text(img, p, cy, alpha, dy=0.0, shadow=0.65):
    if alpha <= 0.002:
        return img
    x0 = int(W / 2 - p["w"] / 2)
    y0 = int(cy - p["h"] / 2 + dy)
    a = p["a"] * alpha
    s = p["sh"] * alpha * shadow
    reg = img[y0:y0 + p["h"], x0:x0 + p["w"]]
    reg = reg * (1 - s[..., None])
    reg = reg * (1 - a[..., None]) + p["col"] * a[..., None]
    img[y0:y0 + p["h"], x0:x0 + p["w"]] = reg
    return img


TEXT = None
# v2: bloque final 2 compases antes (mas tiempo de lectura) y 120 px mas abajo (libre del semaforo)
FINAL_BAR = 6
FINAL_DY = 120


def text_times():
    return {"title": (bar(2) + 0.25, bar(4) - 0.1), "final": bar(FINAL_BAR) + 0.12,
            "avail": bar(FINAL_BAR + 1) + 0.05, "soon": bar(FINAL_BAR + 2) + 0.05}

NO_TEXT = False  # --no-text: placa limpia para montar los textos en CapCut


def texts():
    global TEXT
    if TEXT is None:
        TEXT = {
            "artist_s": text_patch("GÓNGORA", 38, 600, 0.55),
            "title_s": text_patch("A MOTOR PARADO", 64, 300, 0.16),
            "artist_l": text_patch("GÓNGORA", 46, 650, 0.6),
            "title_l": text_patch("A MOTOR PARADO", 84, 250, 0.12, max_w=800),
            "avail": text_patch("YA DISPONIBLE", 34, 600, 0.42),
            "stores": text_patch("Amazon Music · YouTube Music", 46, 400, 0.02),
            "soon": text_patch("YA DISPONIBLE EN SPOTIFY", 33, 500, 0.30, color=(205, 245, 220)),
        }
    return TEXT


def fade_io(t, a, b, fin=0.5, fout=0.5):
    return sm(a, a + fin, t) * (1 - sm(b - fout, b, t))


def overlay_text(img, t):
    T = texts()
    # bloque 1: titulo integrado cuando el coche se para
    tt = text_times()
    t1, t2 = tt["title"]
    a = fade_io(t, t1, t2, 0.7, 0.6)
    if a > 0:
        rise = (1 - sm(t1, t1 + 1.2, t)) * 18
        put_text(img, T["artist_s"], 1150, a, rise)
        put_text(img, T["title_s"], 1225, sm(t1 + 0.25, t1 + 1.0, t) * (1 - sm(t2 - 0.6, t2, t)), rise)
    # bloque final
    tf = tt["final"]
    Y = FINAL_DY
    if t > tf:
        a = sm(tf, tf + 0.8, t)
        rise = (1 - sm(tf, tf + 1.6, t)) * 22
        put_text(img, T["artist_l"], 730 + Y, a, rise)
        put_text(img, T["title_l"], 832 + Y, sm(tf + 0.25, tf + 1.1, t), rise)
        # linea fina
        ta = tt["avail"]
        la = sm(ta - 0.35, ta + 0.25, t)
        if la > 0:
            half = int(70 * la)
            ly = 922 + Y
            img[ly:ly + 2, 540 - half:540 + half] = img[ly:ly + 2, 540 - half:540 + half] * (1 - 0.8 * la) + 0.8 * la
        put_text(img, T["avail"], 1000 + Y, sm(ta, ta + 0.6, t), (1 - sm(ta, ta + 1.0, t)) * 12)
        put_text(img, T["stores"], 1062 + Y, sm(ta + 0.2, ta + 0.9, t), (1 - sm(ta + 0.2, ta + 1.2, t)) * 12)
        ts = tt["soon"]
        put_text(img, T["soon"], 1190 + Y, sm(ts, ts + 0.7, t), (1 - sm(ts, ts + 1.2, t)) * 12)
    return img


# --------------------------------------------------------------------------- escenas
def sc_rush(t):
    """0 - bar1: la cabeza a mil. Ciudad a toda velocidad, cada pulso cambia el punto de fuga."""
    k = int((t - PHASE) // PERIOD) if t >= PHASE else -1
    tb = (t - beat(k)) if k >= 0 else t
    rng = np.random.default_rng(100 + k)
    cx = LW * (0.5 + rng.uniform(-0.18, 0.18))
    cy = LH * (0.42 + rng.uniform(-0.08, 0.08))
    roll = rng.uniform(-0.25, 0.25)
    s, v = distance_curve(t)
    cv = sky(t)
    punch = 1 + 0.5 * math.exp(-tb / 0.08)
    shifts = [None, lambda c: c[[2, 1, 0]] * 0.9 + 0.1, None, lambda c: c * np.array([1.0, 0.5, 0.8], np.float32)]
    CITY.render(cv, s * 1.6, v * 1.6, cx, cy, f=300, shutter=0.05, defocus=0.6, gain=1.2 * punch, roll=roll,
                palette_shift=shifts[k % 4] if k >= 0 else None)
    return cv, dict(drops=0.0, exposure=1.0 + 0.4 * math.exp(-tb / 0.06))


def sc_brake(t):
    """bar1 - bar2: a traves del parabrisas, empieza a llover y el coche frena hasta pararse."""
    s, v = distance_curve(t)
    u = sm(bar(1), bar(2), t)
    cv = sky(t)
    cx = LW * 0.5 + hnoise(t, 1, 4)
    cy = LH * 0.44 + hnoise(t, 2, 3)
    CITY.render(cv, s, v, cx, cy, f=330, shutter=0.04, defocus=1.0 + u * 0.8, gain=1.0)
    car_ahead(cv, brake=u)
    traffic_light(cv, RED, 1.4 * u)
    return cv, dict(drops=0.15 + 0.35 * u, slide=0.3)


def stopped_street(t, cv, defocus=1.8, gain=1.0, light=RED, light_k=1.4, ahead=True, xoff=0.0, zoom=1.0):
    cx = LW * 0.5 + hnoise(t, 3, 2.5) + xoff
    cy = LH * 0.44 + hnoise(t, 4, 2)
    CITY.render(cv, S_STOP, 0.0, cx, cy, f=330 * zoom, defocus=defocus, gain=gain)
    if ahead:
        car_ahead(cv, brake=0.9)
    traffic_light(cv, light, light_k, x=LW * 0.5 + xoff, r=44 * zoom)


CROSS1 = [beat(k) for k in range(8, 16)]
CROSS2 = [beat(k) + PERIOD / 2 * (j % 2) for j, k in enumerate(range(24, 32)) for _ in (0,)]


def sc_stop(t):
    """bar2 - bar3: parado en rojo. Delante, la ciudad sigue cruzando a cada pulso."""
    cv = sky(t)
    stopped_street(t, cv)
    cross_traffic(cv, t, CROSS1)
    return cv, dict(drops=0.55, slide=0.6, scrim=0.8, scrim_y=1190)


def sc_hands(t):
    """bar3 - bar4: sigue parado; el foco se pierde (v2: sin volante)."""
    cv = sky(t)
    stopped_street(t, cv, defocus=3.0, gain=0.8, ahead=True)
    cross_traffic(cv, t, CROSS1)
    return cv, dict(drops=0.6, slide=0.6, exposure=0.95,
                    scrim=0.6 * (1 - sm(bar(4) - 0.6, bar(4), t)), scrim_y=1190)


def sc_window(t):
    """bar4 - bar5: ventanilla lateral. Respiracion que empana el cristal; fuera todo pasa."""
    cv = vgrad((0.02, 0.02, 0.05), (0.06, 0.03, 0.06))
    rng = np.random.default_rng(55)
    n = 70
    xs = rng.uniform(0, 1, n); ys = rng.uniform(0.15, 0.95, n); sp = rng.uniform(40, 160, n)
    cols = [AMBER, MAGENTA, TEAL, WARMW, RED, BLUE]
    for i in range(n):
        x = (xs[i] * (LW + 200) - (t * sp[i])) % (LW + 200) - 100
        r = 10 + 30 * (sp[i] / 160)
        capsule(cv, x, ys[i] * LH, x + sp[i] * 0.05, ys[i] * LH, r, cols[i % len(cols)], 0.55 / (r ** 0.4), soft=1.5,
                ring=0.3)
    # silueta de un paraguas/peaton cruzando por detras, a contraluz
    return cv, dict(drops=0.7, slide=1.4, fog=breath(t), mag=3.2)


def breath(t):
    """Vaho: exhala al inicio de cada compas, se disipa despacio."""
    tb = (t - bar(4)) % BAR
    return sm(0.0, 0.55, tb) * (1 - sm(0.7, BAR, tb)) * 0.85


def sc_flash(t):
    """bar5 - bar6: saturacion. Cortes a pulso, que se doblan en la segunda mitad."""
    t0 = bar(5)
    q = (t - t0) / PERIOD
    idx = int(q) if q < 2 else 2 + int((q - 2) * 2)
    tb = (t - t0) - (idx * PERIOD if idx < 2 else 2 * PERIOD + (idx - 2) * PERIOD / 2)
    variant = idx % 4
    cv = sky(t) * 0.7
    params = dict(drops=0.8, slide=1.0, exposure=1.0 + 0.5 * math.exp(-tb / 0.05))
    if variant == 0:  # intermitente en el salpicadero
        cv = vgrad((0.01, 0.01, 0.02), (0.02, 0.015, 0.02))
        on = ((t - PHASE) % PERIOD) < PERIOD * 0.5
        params["indicator"] = 1.0 if on else 0.08
        params["drops"] = 0.0
        stopped_street(t, cv, defocus=4.0, gain=0.35)
    elif variant == 1:  # semaforo en rojo, muy cerca
        rng = np.random.default_rng(300 + idx)
        for _ in range(14):
            x, y = rng.uniform(0, LW), rng.uniform(0, LH)
            capsule(cv, x, y, x, y, rng.uniform(25, 60), [AMBER, TEAL, MAGENTA][_ % 3], 0.18, soft=2, ring=0.3)
        traffic_light(cv, RED, 2.4, x=LW * 0.5, y=LH * 0.42, r=150)
        params["drops"] = 0.9
        params["mag"] = 3.6
        params["macro"] = 1.8
    elif variant == 2:  # estelas, la ciudad que no para
        CITY2.render(cv, t * 30, 30, LW * 0.5, LH * 0.45, f=280, shutter=0.06, defocus=0.4, gain=1.2,
                     roll=0.5 * ((idx % 3) - 1))
        params["drops"] = 0.3
    else:  # gotas en macro sobre bokeh
        stopped_street(t, cv, defocus=5.0, gain=1.1, xoff=60)
        params["drops"] = 1.0
        params["macro"] = 1.6
    return cv, params


def wiper_cleared(t, t0):
    """Funcion (x,y)->bool: zona barrida por el limpiaparabrisas en el instante t."""
    u = (t - t0) / 0.45
    if u < 0:
        return None, None
    ang = -math.pi * min(u, 1.0)  # de derecha a izquierda, pivote abajo
    px, py = 140.0, H + 120.0
    regrow = (t - t0 - 0.45) / 2.0

    def cleared(x, y, _a=ang):
        a = math.atan2(y - py, x - px)  # en (-pi, 0)
        if a > _a + 0.0:
            return False
        return True

    def cleared_full(x, y):
        if regrow <= 0:
            return cleared(x, y)
        # las gotas vuelven poco a poco
        h = (math.sin(x * 12.9898 + y * 78.233) * 43758.5453) % 1
        return cleared(x, y) and h > regrow

    return cleared_full, ang if u < 1.05 else None


def sc_wait(t):
    """bar6 - bar8: la espera. La ciudad pasa cada vez mas deprisa; respira; el limpia barre."""
    u = sm(bar(6), bar(8), t)
    zoom = 1.0 + 0.12 * u
    cv = sky(t)
    stopped_street(t, cv, defocus=2.0, zoom=zoom, light_k=1.4 + 0.3 * math.sin(t * 2.9))
    cr = [bar(6) + PERIOD * (k * (1 - 0.35 * (k / 16))) for k in range(16)]
    cross_traffic(cv, t, cr, dur=0.55 - 0.2 * u)
    cleared, ang = wiper_cleared(t, beat(28))
    return cv, dict(drops=0.65 + 0.3 * u, slide=1.0, cleared=cleared, wiper_ang=ang,
                    vignette=0.55 + 0.15 * math.sin((t - bar(6)) / BAR * 2 * math.pi),
                    scrim=sm(text_times()["final"], text_times()["final"] + 0.8, t), scrim_y=960 + FINAL_DY)


def sc_green(t):
    """bar8 - bar9: verde. La lluvia afloja, se respira."""
    u = sm(bar(8), bar(8) + 0.6, t)
    cv = sky(t)
    stopped_street(t, cv, defocus=2.4, light=RED, light_k=1.4 * (1 - u), ahead=False)
    traffic_light(cv, GREEN, 1.3 * u, y=LH * 0.215 + 2 * 44 * 1.05)
    car_ahead(cv, brake=0.9 * (1 - u))
    return cv, dict(drops=0.7 - 0.35 * sm(bar(8), bar(9), t), slide=0.4, scrim=1.0, scrim_y=960 + FINAL_DY)


def sc_calm(t):
    """bar9 - final: arranca despacio hacia algo mas claro."""
    tt = t - bar(9)
    s = S_STOP + 1.2 * tt * tt * 0.5 + 0.3 * tt
    v = 1.2 * tt + 0.3
    dawn = sm(bar(9), DUR, t)
    cv = sky(t) * (1 - dawn) + sky(t, "dawn") * dawn
    CITY.render(cv, s, v, LW * 0.5 + hnoise(t, 9, 3), LH * 0.44, f=330, defocus=3.0, gain=0.9 - 0.3 * dawn,
                reflect=True)
    traffic_light(cv, GREEN, 1.0 * (1 - sm(bar(9), bar(10), t)), y=LH * 0.215 + 92 - tt * 25)
    return cv, dict(drops=0.35 * (1 - dawn), slide=0.2, scrim=1.0, scrim_y=960 + FINAL_DY, exposure=1.0 + 0.15 * dawn)


SCENES = [
    (0.0, bar(1), sc_rush),
    (bar(1), bar(2), sc_brake),
    (bar(2), bar(3), sc_stop),
    (bar(3), bar(4), sc_hands),
    (bar(4), bar(5), sc_window),
    (bar(5), bar(6), sc_flash),
    (bar(6), bar(8), sc_wait),
    (bar(8), bar(9), sc_green),
    (bar(9), DUR + 1, sc_calm),
]


def indicator(img, k):
    """Testigo del intermitente en el cuadro: pequeno, verde, ligeramente desenfocado."""
    cx, cy, s = 600, 1240, 46
    pts = [(cx - s, cy - 0.3 * s), (cx + 0.15 * s, cy - 0.3 * s), (cx + 0.15 * s, cy - 0.75 * s), (cx + s, cy),
           (cx + 0.15 * s, cy + 0.75 * s), (cx + 0.15 * s, cy + 0.3 * s), (cx - s, cy + 0.3 * s)]
    im = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(im)
    d.polygon(pts, fill=255)
    d.polygon([(2 * 600 - 120 - x, y) for x, y in pts], fill=40)  # el otro, apagado
    # arco del cuadro de instrumentos
    d.arc([140, 1060, 940, 1860], 200, 340, fill=70, width=5)
    m = np.asarray(im.filter(ImageFilter.GaussianBlur(4)), np.float32)[..., None] / 255
    g = blur(m[::4, ::4], 8).repeat(4, 0).repeat(4, 1)
    col = np.array([0.25, 1.0, 0.45], np.float32)
    base = np.array([0.35, 0.6, 0.9], np.float32) * 0.12
    return img + m * (col * 1.3 * k + base * (1 - k)) + g * col * 1.4 * k


def render_frame(i):
    t = i / FPS
    for a, b, fn in SCENES:
        if a <= t < b:
            break
    lo, p = fn(t)
    lo = glow(lo)
    img = up(lo).astype(np.float32)
    if p.get("fog"):
        f = p["fog"]
        ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
        d = ((xs - 520) / 520) ** 2 + ((ys - 1000) / 640) ** 2
        fa = np.clip((1.25 - d) * 1.4, 0, 1) * f
        foggy = blur(img[::4, ::4], 9).repeat(4, 0).repeat(4, 1) * 1.25 + 0.045
        img = img * (1 - fa[..., None]) + foggy * fa[..., None]
    if p.get("drops", 0) > 0:
        img = RAIN.apply(img, t, p["drops"], p.get("slide", 1.0), p.get("cleared"), p.get("mag", 2.6),
                         p.get("macro", 1.0))
    if p.get("wiper_ang") is not None:
        img = draw_wiper(img, p["wiper_ang"])
    if p.get("wheel"):
        img = apply_wheel(img, (0.9, 0.1, 0.08), p.get("sweep_x"), p.get("sweep_k", 0))
    if p.get("indicator") is not None:
        img = indicator(img, p["indicator"])
    out = tonemap(img, p.get("exposure", 1.0))
    # vineta
    ys, xs = np.ogrid[0:H, 0:W]
    vig = ((xs - W / 2) / (W * 0.62)) ** 2 + ((ys - H / 2) / (H * 0.6)) ** 2
    out = out * (1 - p.get("vignette", 0.5) * np.clip(vig, 0, 1.4)[..., None] * 0.6)
    # velo suave para legibilidad del texto final
    if p.get("scrim"):
        band = np.exp(-((ys - p.get("scrim_y", 960)) / 380.0) ** 2)
        out = out * (1 - 0.35 * p["scrim"] * band[..., None])
    if not NO_TEXT:
        out = overlay_text(out, t)
    # grano
    rng = np.random.default_rng(1000 + i)
    g = rng.standard_normal((H // 2, W // 2), dtype=np.float32).repeat(2, 0).repeat(2, 1)
    out = out + g[..., None] * 0.018
    # fundido final
    out = out * (1 - 0.0 * sm(DUR - 0.3, DUR, t))
    return (np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8)


def draw_wiper(img, ang):
    px, py = 140.0, H + 120.0
    L = 2300
    ex, ey = px + L * math.cos(ang), py + L * math.sin(ang)
    im = Image.new("L", (W, H), 0)
    ImageDraw.Draw(im).line([(px, py), (ex, ey)], fill=255, width=46)
    m = np.asarray(im.filter(ImageFilter.GaussianBlur(5)), np.float32)[..., None] / 255
    return img * (1 - m * 0.97) + m * 0.01


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stills", nargs="*", type=float)
    ap.add_argument("--out", default=str(OUTDIR / "a_motor_parado_teaser_v2.mp4"))
    ap.add_argument("--no-text", action="store_true", help="render sin textos (placa para CapCut)")
    args = ap.parse_args()
    global NO_TEXT
    NO_TEXT = args.no_text
    if args.stills:
        d = WORK / "stills"
        d.mkdir(parents=True, exist_ok=True)
        for s in args.stills:
            Image.fromarray(render_frame(int(round(s * FPS)))).save(d / f"t{s:05.2f}.jpg", quality=90)
            print("still", s)
        return
    audio = WORK / "a_motor_parado_0049_0113.flac"
    cmd = [FF, "-v", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", str(audio),
           "-map", "0:v", "-map", "1:a",
           "-c:v", "libx264", "-preset", "slow", "-crf", "17", "-pix_fmt", "yuv420p", "-profile:v", "high",
           "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
           "-af", "afade=t=in:st=0:d=0.01,afade=t=out:st=23.6:d=0.4",
           "-c:a", "aac", "-b:a", "320k", "-ar", "44100",
           "-t", f"{DUR:.3f}", "-movflags", "+faststart", args.out]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    with Pool(6) as pool:
        for n, fr in enumerate(pool.imap(render_frame, range(NFRAMES), chunksize=2)):
            proc.stdin.write(fr.tobytes())
            if n % 30 == 0:
                print(f"frame {n}/{NFRAMES}", flush=True)
    proc.stdin.close()
    sys.exit(proc.wait())


if __name__ == "__main__":
    main()
