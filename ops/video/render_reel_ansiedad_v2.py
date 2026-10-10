"""Fotogramas de dirección de arte v2 para el reel «A motor parado».

Combina una placa fotográfica nocturna sin texto con interfaz y tipografía
procedurales. Los mensajes permanecen editables como datos y capas.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path
import subprocess

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont


W, H = 1080, 1920
ROOT = Path("output/social/a_motor_parado/reel_ansiedad_v2")
ASSETS = ROOT / "assets"
SAMPLES = ROOT / "samples"
MASTER = ROOT / "a_motor_parado_ansiedad_reel_v2.mp4"
AUDIO = Path("output/social/a_motor_parado/reel_ansiedad/work/a_motor_parado_0048_0114.wav")
PHONE = ASSETS / "nightstand_phone.png"
COVER = ASSETS / "a_motor_parado_cover.jpg"
REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

INK = (7, 14, 28)
WHITE = (245, 247, 252)
MUTED = (157, 178, 201)
TEAL = (77, 190, 183)
MAGENTA = (204, 83, 139)
FPS, DURATION = 30, 26.0
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

MESSAGES = [
    ("in", "¿Estás despierto?"),
    ("out", "Estaba a punto de dormirme."),
    ("in", "¿Y si mañana sale todo mal?"),
    ("out", "Ahora no, por favor."),
    ("in", "Y lo que dijiste hoy…\n¿seguro que no les sentó mal?"),
    ("out", "Le he dado mil vueltas."),
    ("in", "Pues vamos a darle otra."),
    ("out", "No ha pasado nada.\nY ya me has quitado el sueño."),
]


def f(size, bold=False):
    return ImageFont.truetype(BOLD if bold else REGULAR, size)


def fit_cover(image, size=(W, H)):
    iw, ih = image.size
    scale = max(size[0] / iw, size[1] / ih)
    nw, nh = round(iw * scale), round(ih * scale)
    image = image.resize((nw, nh), Image.Resampling.LANCZOS)
    x, y = (nw - size[0]) // 2, (nh - size[1]) // 2
    return image.crop((x, y, x + size[0], y + size[1]))


def grade(image, darkness=.30):
    image = fit_cover(image.convert("RGB"))
    arr = np.asarray(image, dtype=np.float32)
    arr[..., 0] *= .78
    arr[..., 1] *= .88
    arr[..., 2] *= 1.08
    arr *= darkness
    return Image.fromarray(np.uint8(np.clip(arr, 0, 255)), "RGB")


def homography_coeffs(dst, src):
    """Coeficientes output→input para Image.Transform.PERSPECTIVE."""
    a, b = [], []
    for (x, y), (u, v) in zip(dst, src):
        a.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.append(u)
        a.append([0, 0, 0, x, y, 1, -v * x, -v * y]); b.append(v)
    return np.linalg.solve(np.asarray(a), np.asarray(b)).tolist()


def opener(show_notification=True):
    """Fotografía editorial con la interfaz integrada en perspectiva."""
    base = fit_cover(Image.open(PHONE)).convert("RGBA")
    # La escena se oscurece ligeramente para que el encendido del móvil mande.
    shade = Image.new("RGBA", (W, H), (2, 7, 17, 42))
    base = Image.alpha_composite(base, shade)

    ui = Image.new("RGBA", (600, 950), (3, 10, 22, 235))
    d = ImageDraw.Draw(ui, "RGBA")
    d.text((300, 132), "03:17", font=f(84, True), anchor="mm", fill=(*WHITE, 255))
    d.text((300, 195), "viernes, 9 de octubre", font=f(22), anchor="mm", fill=(*MUTED, 220))
    if show_notification:
        # Notificación con brillo contenido y acento de campaña.
        d.rounded_rectangle((54, 350, 546, 535), 32, fill=(21, 34, 54, 244), outline=(*MAGENTA, 145), width=3)
        d.ellipse((80, 382, 126, 428), fill=(*MAGENTA, 220))
        d.text((146, 406), "MENSAJES", font=f(21, True), anchor="lm", fill=(*MUTED, 235))
        d.text((84, 477), "¿Estás despierto?", font=f(34), anchor="lm", fill=(*WHITE, 255))
        d.text((516, 406), "ahora", font=f(20), anchor="rm", fill=(*MUTED, 205))
    # Aproximación de la pantalla fotografiada tras escalar la placa a 1080×1920.
    dst = [(148, 974), (526, 928), (967, 1398), (387, 1467)]
    src = [(0, 0), (600, 0), (600, 950), (0, 950)]
    warped = ui.transform((W, H), Image.Transform.PERSPECTIVE, homography_coeffs(dst, src),
                          resample=Image.Resampling.BICUBIC)
    base = Image.alpha_composite(base, warped)
    # Firma mínima de campaña, fuera del teléfono y lejos de la UI de Instagram.
    o = Image.new("RGBA", (W, H), (0, 0, 0, 0)); od = ImageDraw.Draw(o)
    od.text((78, 160), "A MOTOR PARADO", font=f(23, True), fill=(230, 235, 242, 190))
    od.line((78, 205, 310, 205), fill=(*TEAL, 150), width=3)
    return Image.alpha_composite(base, o).convert("RGB")


def campaign_backdrop():
    """Cruza la fotografía con la textura cromática de la portada."""
    room = grade(Image.open(PHONE), .31).filter(ImageFilter.GaussianBlur(13))
    cover = fit_cover(Image.open(COVER)).filter(ImageFilter.GaussianBlur(8)).convert("RGBA")
    cover.putalpha(80)
    return Image.alpha_composite(room.convert("RGBA"), cover)


def glow(layer, box, color, blur=70, alpha=90):
    g = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    ImageDraw.Draw(g).ellipse(box, fill=(*color, alpha))
    return Image.alpha_composite(layer, g.filter(ImageFilter.GaussianBlur(blur)))


def draw_bubble(layer, side, text, y, newest=False):
    d = ImageDraw.Draw(layer, "RGBA")
    font = f(36)
    lines = text.split("\n")
    widths = [d.textlength(line, font=font) for line in lines]
    bw = int(max(widths) + 72)
    bh = 72 + (len(lines) - 1) * 47
    x0 = 106 if side == "in" else 900 - bw
    x1 = x0 + bw
    accent = MAGENTA if side == "in" else TEAL
    fill = (19, 30, 48, 238) if side == "in" else (27, 56, 65, 242)
    if newest:
        layer = glow(layer, (x0 - 45, y - 55, x1 + 45, y + bh + 55), accent, 55, 55)
        d = ImageDraw.Draw(layer, "RGBA")
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle((x0, y + 8, x1, y + bh + 12), 29, fill=(0, 0, 0, 100))
    layer = Image.alpha_composite(layer, shadow.filter(ImageFilter.GaussianBlur(14)))
    d = ImageDraw.Draw(layer, "RGBA")
    d.rounded_rectangle((x0, y, x1, y + bh), 29, fill=fill, outline=(*accent, 165), width=2)
    d.rounded_rectangle((x0 + 1, y + 10, x0 + 7, y + bh - 10), 3, fill=(*accent, 220))
    for i, line in enumerate(lines):
        d.text((x0 + 38, y + 36 + i * 47), line, font=font, anchor="lm", fill=(*WHITE, 255))
    return layer, y + bh


def chat(message_count=5, reveal=False):
    img = campaign_backdrop()
    img = glow(img, (650, 180, 1120, 760), MAGENTA, 120, 34)
    img = glow(img, (-120, 900, 420, 1600), TEAL, 140, 26)
    d = ImageDraw.Draw(img, "RGBA")
    # Panel de cristal: ocupa pantalla, pero conserva profundidad fotográfica.
    d.rounded_rectangle((48, 98, 936, 1790), 54, fill=(5, 13, 28, 204), outline=(168, 192, 218, 64), width=2)
    d.text((88, 155), "03:17", font=f(28, True), fill=(*WHITE, 220))
    d.text((884, 155), "•••", font=f(23, True), anchor="ra", fill=(*MUTED, 180))
    d.line((90, 214, 894, 214), fill=(132, 157, 183, 58), width=2)

    top = 300
    if reveal:
        d.rounded_rectangle((82, 248, 902, 386), 34, fill=(15, 27, 45, 245), outline=(*MAGENTA, 150), width=2)
        d.ellipse((111, 273, 201, 363), fill=(28, 39, 60, 255), outline=(*MAGENTA, 185), width=3)
        d.ellipse((138, 300, 174, 336), outline=(*TEAL, 210), width=4)
        d.text((232, 282), "Ansiedad", font=f(44, True), fill=(*WHITE, 255))
        d.text((234, 337), "EN LÍNEA", font=f(20, True), fill=(*MAGENTA, 230))
        top = 460

    visible = MESSAGES[:message_count]
    heights = [72 + 47 * text.count("\n") for _, text in visible]
    total = sum(heights) + 22 * max(0, len(heights) - 1)
    available = 1480 - top
    y = top + max(0, available - total)
    for i, (side, text) in enumerate(visible):
        img, y = draw_bubble(img, side, text, y, newest=i == len(visible) - 1)
        y += 22
    # Pie visual, no caja de formulario genérica.
    d = ImageDraw.Draw(img, "RGBA")
    d.line((108, 1642, 876, 1642), fill=(133, 157, 181, 72), width=2)
    d.text((108, 1686), "A MOTOR PARADO", font=f(21, True), fill=(*MUTED, 190))
    d.ellipse((838, 1661, 876, 1699), fill=(*TEAL, 180))
    return img.convert("RGB")


def end_card():
    cover = Image.open(COVER).convert("RGB")
    bg = fit_cover(cover.resize((1600, 1600))).filter(ImageFilter.GaussianBlur(32))
    arr = np.asarray(bg, dtype=np.float32) * np.array([.42, .35, .50], dtype=np.float32)
    bg = Image.fromarray(np.uint8(np.clip(arr, 0, 255)), "RGB").convert("RGBA")
    bg = glow(bg, (570, 130, 1160, 760), MAGENTA, 110, 60)
    bg = glow(bg, (-180, 860, 460, 1510), TEAL, 120, 50)

    art = cover.resize((620, 620), Image.Resampling.LANCZOS).convert("RGBA")
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((220, 248, 860, 888), 20, fill=(0, 0, 0, 180))
    bg = Image.alpha_composite(bg, shadow.filter(ImageFilter.GaussianBlur(30)))
    bg.alpha_composite(art, (230, 230))
    d = ImageDraw.Draw(bg, "RGBA")
    d.rounded_rectangle((228, 228, 852, 852), 8, outline=(235, 240, 247, 105), width=2)
    d.text((116, 1000), "A MOTOR", font=f(76, True), fill=(*WHITE, 255))
    d.text((116, 1087), "PARADO", font=f(104, True), fill=(232, 238, 245, 255))
    d.text((122, 1225), "GÓNGORA", font=f(35, True), fill=(*MUTED, 255))
    d.rounded_rectangle((116, 1365, 875, 1460), 24, fill=(7, 18, 28, 225), outline=(*TEAL, 190), width=2)
    d.rectangle((116, 1365, 126, 1460), fill=(*TEAL, 240))
    d.text((154, 1413), "YA DISPONIBLE EN SPOTIFY", font=f(29, True), anchor="lm", fill=(*WHITE, 255))
    d.text((120, 1540), "ENLACES EN MI BIO  →", font=f(31, True), fill=(218, 226, 236, 245))
    d.text((120, 1620), "FUIMOS DOS", font=f(20, True), fill=(*MAGENTA, 220))
    return bg.convert("RGB")


def smoothstep(value):
    value = max(0.0, min(1.0, value))
    return value * value * (3 - 2 * value)


def push_in(image, scale, cx=.52, cy=.57):
    """Movimiento óptico mínimo para que la fotografía no parezca un still."""
    if scale <= 1.0001:
        return image
    nw, nh = int(W / scale), int(H / scale)
    x0 = int((W - nw) * cx)
    y0 = int((H - nh) * cy)
    return image.crop((x0, y0, x0 + nw, y0 + nh)).resize((W, H), Image.Resampling.LANCZOS)


def timeline_states():
    """Entradas ajustadas a los ataques medidos del fragmento musical."""
    return [
        (0.00, 0.00, opener(False)),
        (0.34, 0.24, opener(True)),
        (1.42, 0.72, chat(2, False)),
        (3.20, 0.25, chat(3, False)),
        (6.42, 0.25, chat(4, False)),
        (8.03, 0.28, chat(5, False)),
        (12.49, 0.26, chat(6, False)),
        (15.35, 0.28, chat(7, False)),
        (18.17, 0.70, chat(7, True)),
        (21.06, 0.28, chat(8, True)),
        (24.04, 0.42, end_card()),
    ]


def frame_at(t, states):
    current = states[0][2]
    previous = current
    start, transition = 0.0, 0.0
    for event_start, event_transition, event_image in states[1:]:
        if t < event_start:
            break
        previous = current
        current = event_image
        start, transition = event_start, event_transition
    # El plano inicial se acerca un 2,2 %; el resto respira apenas un 0,4 %.
    if t < 1.42:
        scale = 1.0 + .022 * (t / 1.42)
        previous = push_in(previous, scale)
        current = push_in(current, scale)
    elif t < 24.04:
        scale = 1.004 + .002 * math.sin(t * .42)
        previous = push_in(previous, scale)
        current = push_in(current, scale)
    if transition and t < start + transition:
        return Image.blend(previous, current, smoothstep((t - start) / transition))
    return current


def render_master(out=MASTER):
    if not AUDIO.exists():
        raise SystemExit(f"Falta el fragmento de audio: {AUDIO}")
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
        "-i", str(AUDIO), "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "256k", "-ar", "44100", "-ac", "2",
        "-t", f"{DURATION:.3f}", "-movflags", "+faststart", "-y", str(out),
    ]
    states = timeline_states()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        for n in range(round(DURATION * FPS)):
            proc.stdin.write(frame_at(n / FPS, states).tobytes())
    finally:
        if proc.stdin:
            proc.stdin.close()
    if proc.wait() != 0:
        raise SystemExit("ffmpeg no pudo codificar el vídeo v2")
    print(out)


def save_samples():
    SAMPLES.mkdir(parents=True, exist_ok=True)
    frames = {
        "01_apertura_cinematografica.png": opener(),
        "02_chat_campana.png": chat(5, False),
        "03_revelacion_ansiedad.png": chat(7, True),
        "04_cierre_spotify.png": end_card(),
    }
    for name, image in frames.items():
        image.save(SAMPLES / name, quality=95)
        print(SAMPLES / name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", choices=("samples", "render"), default="samples")
    parser.add_argument("--out", type=Path, default=MASTER)
    args = parser.parse_args()
    save_samples() if args.mode == "samples" else render_master(args.out)
