"""Diseño y render del reel nocturno de «A motor parado».

La interfaz se dibuja de forma procedural: los textos, burbujas y tiempos viven
como datos editables en este archivo. No usa capturas de una aplicación real.

Genera los tres fotogramas de aprobación y el máster vertical de 26 segundos.
Los textos y tiempos permanecen definidos como datos editables, nunca como una
captura de una aplicación de mensajería real.
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
OUT = Path("output/social/a_motor_parado/reel_ansiedad/samples")
ROOT = Path("output/social/a_motor_parado/reel_ansiedad")
AUDIO = ROOT / "work/a_motor_parado_0048_0114.wav"
MASTER = ROOT / "a_motor_parado_ansiedad_reel_v1.mp4"
PLATE = ROOT / "capcut_editable/plate_9x16_sin_textos.mp4"
FONT = "/usr/share/fonts/truetype/ubuntu/UbuntuSans[wdth,wght].ttf"
FPS, DURATION = 30, 26.0
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# Instagram: se reserva especialmente el lateral derecho y el tercio inferior.
SAFE_LEFT, SAFE_RIGHT = 76, 900
CHAT_TOP, CHAT_BOTTOM = 285, 1560

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


def font(size: int):
    return ImageFont.truetype(FONT, size=size)


def gradient(top, bottom):
    y = np.linspace(0, 1, H, dtype=np.float32)[:, None, None]
    a = np.asarray(top, dtype=np.float32)[None, None]
    b = np.asarray(bottom, dtype=np.float32)[None, None]
    arr = np.broadcast_to(a * (1 - y) + b * y, (H, W, 3)).copy()
    return arr


def night_backdrop(seed=317):
    """Fondo nocturno cotidiano: pared azul y una lámpara cálida desenfocada."""
    rng = np.random.default_rng(seed)
    arr = gradient((7, 13, 28), (15, 25, 40))
    yy, xx = np.mgrid[:H, :W]
    warm = np.exp(-(((xx - 835) / 240) ** 2 + ((yy - 485) / 330) ** 2))
    arr += warm[..., None] * np.array([44, 24, 7], dtype=np.float32)
    cool = np.exp(-(((xx - 170) / 460) ** 2 + ((yy - 900) / 700) ** 2))
    arr += cool[..., None] * np.array([0, 6, 14], dtype=np.float32)
    # Textura apenas perceptible para evitar un degradado digital plano.
    noise = rng.normal(0, 1.25, (H, W, 1)).astype(np.float32)
    arr += noise
    img = Image.fromarray(np.uint8(np.clip(arr, 0, 255)), "RGB")
    # Mesilla, deliberadamente sobria y sin atrezo reconocible.
    d = ImageDraw.Draw(img, "RGBA")
    d.polygon([(0, 1380), (W, 1260), (W, H), (0, H)], fill=(12, 16, 23, 245))
    d.line([(0, 1380), (W, 1260)], fill=(74, 62, 54, 65), width=3)
    return img


def rounded_mask(size, radius):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius, fill=255)
    return m


def phone_frame(show_notification=True, notification_text=True):
    """Plano de apertura: móvil realista en la mesilla, sin manos ni personas."""
    bg = night_backdrop().filter(ImageFilter.GaussianBlur(0.45))
    # Sombra del teléfono.
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle((210, 355, 857, 1663), 78, fill=(0, 0, 0, 190))
    shadow = shadow.filter(ImageFilter.GaussianBlur(32))
    bg = Image.alpha_composite(bg.convert("RGBA"), shadow)

    phone = Image.new("RGBA", (650, 1310), (0, 0, 0, 0))
    pd = ImageDraw.Draw(phone, "RGBA")
    pd.rounded_rectangle((8, 8, 642, 1302), 72, fill=(5, 7, 11, 255), outline=(74, 78, 86, 220), width=5)
    pd.rounded_rectangle((25, 25, 625, 1285), 58, fill=(8, 15, 28, 255))
    # Reflejo tenue de la lámpara sobre el cristal.
    glow = Image.new("RGBA", phone.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((390, -110, 800, 390), fill=(215, 149, 70, 38))
    glow = glow.filter(ImageFilter.GaussianBlur(70))
    phone = Image.alpha_composite(phone, glow)
    pd = ImageDraw.Draw(phone, "RGBA")
    # Hora y fecha: la hora es el dato narrativo importante.
    pd.text((325, 215), "03:17", font=font(104), anchor="mm", fill=(239, 243, 250, 245))
    pd.text((325, 292), "viernes, 9 de octubre", font=font(25), anchor="mm", fill=(196, 205, 219, 190))
    if show_notification:
        # Notificación neutra, sin nombre o avatar humano.
        pd.rounded_rectangle((66, 455, 584, 642), 34, fill=(34, 44, 61, 235), outline=(108, 127, 151, 70), width=2)
        pd.ellipse((94, 490, 140, 536), fill=(99, 122, 151, 170))
        pd.text((161, 511), "MENSAJES", font=font(22), anchor="lm", fill=(183, 196, 214, 210))
        if notification_text:
            pd.text((94, 579), "¿Estás despierto?", font=font(34), anchor="lm", fill=(246, 248, 252, 255))
        pd.text((545, 511), "ahora", font=font(21), anchor="rm", fill=(163, 178, 198, 190))
    pd.rounded_rectangle((268, 1238, 382, 1248), 5, fill=(236, 239, 244, 190))
    bg.alpha_composite(phone, (215, 340))

    # Un leve viñeteado da intimidad sin convertirlo en estética de terror.
    arr = np.asarray(bg.convert("RGB"), dtype=np.float32)
    yy, xx = np.mgrid[:H, :W]
    rr = ((xx - W / 2) / (W * .72)) ** 2 + ((yy - H / 2) / (H * .72)) ** 2
    arr *= np.clip(1.06 - rr[..., None] * .34, .66, 1)
    return Image.fromarray(np.uint8(np.clip(arr, 0, 255)), "RGB")


def bubble(draw, side, text, y):
    """Dibuja una burbuja completa y devuelve su borde inferior."""
    f = font(38)
    lines = text.split("\n")
    widths = [draw.textlength(line, font=f) for line in lines]
    bw = int(max(widths) + 62)
    bh = 66 + (len(lines) - 1) * 48
    if side == "in":
        x0 = SAFE_LEFT + 8
        fill, outline, color = (36, 49, 68, 244), (91, 112, 140, 105), (244, 247, 252, 255)
    else:
        x0 = SAFE_RIGHT - bw
        fill, outline, color = (47, 78, 91, 246), (98, 145, 156, 110), (248, 250, 250, 255)
    x1 = x0 + bw
    draw.rounded_rectangle((x0, y, x1, y + bh), 28, fill=fill, outline=outline, width=2)
    for i, line in enumerate(lines):
        draw.text((x0 + 31, y + 33 + i * 48), line, font=f, anchor="lm", fill=color)
    return y + bh


def chat_frame(message_count: int, reveal=False, header_text=True):
    """Chat ampliado; la cabecera solo entra en cuadro durante la revelación."""
    img = Image.fromarray(np.uint8(np.clip(gradient((7, 14, 27), (15, 29, 43)), 0, 255)), "RGB").convert("RGBA")
    # Luz cálida ambiental fuera de foco, visible bajo el cristal ficticio.
    ambient = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ad = ImageDraw.Draw(ambient)
    ad.ellipse((710, 210, 1110, 680), fill=(212, 133, 50, 28))
    ambient = ambient.filter(ImageFilter.GaussianBlur(95))
    img = Image.alpha_composite(img, ambient)
    d = ImageDraw.Draw(img, "RGBA")

    # Barra del sistema: sitúa la escena a las 03:17 sin invadir la narrativa.
    d.text((SAFE_LEFT, 120), "03:17", font=font(30), anchor="lm", fill=(213, 221, 234, 220))
    d.ellipse((822, 108, 838, 124), fill=(198, 210, 225, 205))
    d.arc((852, 104, 884, 136), 205, 335, fill=(198, 210, 225, 205), width=4)
    d.rounded_rectangle((902, 107, 958, 131), 7, outline=(198, 210, 225, 205), width=3)
    d.rectangle((958, 114, 963, 124), fill=(198, 210, 225, 205))

    if reveal:
        # Cabecera compacta y completamente contenida en el marco seguro.
        # El avatar abstracto queda dentro de la barra, como en una app real.
        d.rounded_rectangle((52, 205, 930, 352), 32, fill=(17, 28, 43, 242), outline=(84, 103, 128, 88), width=2)
        d.ellipse((84, 219, 204, 339), fill=(24, 37, 55, 255))
        d.ellipse((116, 251, 172, 307), outline=(101, 128, 154, 175), width=4)
        if header_text:
            d.text((236, 238), "Ansiedad", font=font(46), anchor="la", fill=(250, 251, 253, 255))
            d.text((238, 298), "en línea", font=font(24), anchor="la", fill=(144, 166, 190, 205))
        top = 405
    else:
        # Antes de la revelación, el encuadre comienza bajo la cabecera.
        top = CHAT_TOP

    visible = MESSAGES[:message_count]
    heights = [66 + (m[1].count("\n")) * 48 for m in visible]
    total = sum(heights) + max(0, len(heights) - 1) * 24
    available = CHAT_BOTTOM - top
    y = top + max(0, available - total)
    # Si ya hay muchos mensajes, los anteriores siguen visibles parcialmente al desplazarse.
    if total > available:
        y = top - (total - available)
    for side, text in visible:
        y = bubble(d, side, text, y) + 24

    # Campo de escritura, sobrio y dentro de la zona segura vertical.
    d.rounded_rectangle((SAFE_LEFT, 1642, SAFE_RIGHT, 1730), 42, fill=(21, 33, 48, 235), outline=(81, 102, 126, 85), width=2)
    d.text((116, 1686), "Mensaje", font=font(31), anchor="lm", fill=(132, 149, 171, 170))
    d.ellipse((822, 1662, 890, 1710), fill=(57, 84, 103, 210))
    d.polygon([(846, 1673), (873, 1686), (846, 1699)], fill=(226, 233, 240, 220))

    # Viñeta mínima, mantiene el centro de lectura luminoso.
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
    yy, xx = np.mgrid[:H, :W]
    vign = 1 - .18 * np.clip((((xx - 510) / 700) ** 2 + ((yy - 950) / 1200) ** 2), 0, 1)
    arr *= vign[..., None]
    return Image.fromarray(np.uint8(np.clip(arr, 0, 255)), "RGB")


def closing_frame(with_text=True):
    """Cierre integrado; refleja la disponibilidad actual y la bio del perfil."""
    img = Image.fromarray(np.uint8(np.clip(gradient((7, 14, 27), (15, 29, 43)), 0, 255)), "RGB").convert("RGBA")
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((640, 320, 1140, 900), fill=(210, 132, 50, 34))
    glow = glow.filter(ImageFilter.GaussianBlur(125))
    img = Image.alpha_composite(img, glow)
    d = ImageDraw.Draw(img, "RGBA")
    d.line((132, 725, 868, 725), fill=(109, 135, 158, 90), width=2)
    if with_text:
        d.text((500, 825), "A motor parado", font=font(66), anchor="mm", fill=(249, 250, 252, 255))
        d.text((500, 910), "GÓNGORA", font=font(40), anchor="mm", fill=(177, 199, 218, 235))
        d.text((500, 1075), "Ya disponible en Spotify", font=font(38), anchor="mm", fill=(245, 247, 250, 245))
        d.text((500, 1140), "Enlaces en mi bio", font=font(34), anchor="mm", fill=(164, 184, 204, 225))
    d.line((132, 1245, 868, 1245), fill=(109, 135, 158, 90), width=2)
    return img.convert("RGB")


def smoothstep(value):
    value = max(0.0, min(1.0, value))
    return value * value * (3 - 2 * value)


def push_in(image, scale):
    """Acercamiento óptico muy leve, centrado en el teléfono."""
    if scale <= 1.0001:
        return image
    nw, nh = int(W / scale), int(H / scale)
    x0 = (W - nw) // 2
    y0 = int((H - nh) * .48)
    return image.crop((x0, y0, x0 + nw, y0 + nh)).resize((W, H), Image.Resampling.LANCZOS)


def timeline_states():
    """Estados completos; las transiciones breves coinciden con ataques del clip."""
    return [
        (0.00, 0.00, phone_frame(False)),
        (0.34, 0.26, phone_frame(True)),
        (1.42, 0.66, chat_frame(2, False)),
        (3.20, 0.28, chat_frame(3, False)),
        (6.42, 0.28, chat_frame(4, False)),
        (8.03, 0.30, chat_frame(5, False)),
        (12.49, 0.28, chat_frame(6, False)),
        (15.35, 0.30, chat_frame(7, False)),
        (18.17, 0.72, chat_frame(7, True)),
        (21.06, 0.30, chat_frame(8, True)),
        (24.04, 0.38, closing_frame()),
    ]


def plate_states():
    """Placa para CapCut: conserva UI y fondos, pero no texto narrativo."""
    return [
        (0.00, 0.00, phone_frame(False)),
        (0.34, 0.26, phone_frame(True, notification_text=False)),
        (1.42, 0.66, chat_frame(0, False)),
        (18.17, 0.72, chat_frame(0, True, header_text=False)),
        (24.04, 0.38, closing_frame(False)),
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
    if start < 1.42:
        previous = push_in(previous, 1.0 + min(t, 1.42) * .012)
        if current is states[1][2]:
            current = push_in(current, 1.0 + min(t, 1.42) * .012)
    if transition and t < start + transition:
        p = smoothstep((t - start) / transition)
        return Image.blend(previous, current, p)
    return current


def render_master(out=MASTER):
    """Codifica vídeo H.264 + audio AAC sin alterar el fragmento original."""
    if not AUDIO.exists():
        raise SystemExit(f"Falta el audio exacto: {AUDIO}")
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
        raise SystemExit("ffmpeg no pudo codificar el máster")
    print(out)


def render_plate(out=PLATE):
    """Vídeo base para CapCut; el audio y los textos se añaden en pistas aparte."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-t", f"{DURATION:.3f}", "-movflags", "+faststart", "-an", "-y", str(out),
    ]
    states = plate_states()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        for n in range(round(DURATION * FPS)):
            proc.stdin.write(frame_at(n / FPS, states).tobytes())
    finally:
        if proc.stdin:
            proc.stdin.close()
    if proc.wait() != 0:
        raise SystemExit("ffmpeg no pudo codificar la placa de CapCut")
    print(out)


def save_samples():
    OUT.mkdir(parents=True, exist_ok=True)
    frames = {
        "01_comienzo_01.57s.png": phone_frame(),
        "02_conversacion_10.70s.png": chat_frame(5, reveal=False),
        "03_revelacion_19.24s.png": chat_frame(7, reveal=True),
    }
    for name, image in frames.items():
        image.save(OUT / name, quality=95)
        print(OUT / name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="?", choices=("samples", "render", "plate"), default="samples")
    parser.add_argument("--out", type=Path, default=MASTER)
    args = parser.parse_args()
    if args.mode == "samples":
        save_samples()
    elif args.mode == "render":
        render_master(args.out)
    else:
        render_plate(args.out if args.out != MASTER else PLATE)
