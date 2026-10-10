"""Kit de "A motor parado" para CapCut: capas, draft MP4 por ffmpeg y borrador de CapCut generado como fichero.

No controla la interfaz de CapCut. Escribe un proyecto (borrador) en la carpeta de borradores de
CapCut Desktop para que aparezca en su lista y se pueda retocar/exportar a mano.

Uso (desde la raiz del repo, despues de render_teaser.py --no-text):
    ops/video/.venv/bin/python ops/video/capcut_package.py layers     # PNG de texto + manifiesto
    ops/video/.venv/bin/python ops/video/capcut_package.py ffmpeg     # draft MP4 = placa + capas + audio
    ops/video/.venv/bin/python ops/video/capcut_package.py capcut     # borrador en CapCut Desktop
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import render_teaser as rt  # noqa: E402

FF = rt.FF
KIT = rt.OUTDIR / "capcut_kit"
PLATE = KIT / "plate_9x16_notext.mp4"
AUDIO_SRC = rt.WORK / "a_motor_parado_0049_0113.flac"
AUDIO = KIT / "a_motor_parado_0049_0113.wav"
DRAFT_MP4 = KIT / "ffmpeg_draft_v2.mp4"
MANIFEST = KIT / "manifest.json"

CAPCUT_ROOT_WIN = "C:/Users/pedro/AppData/Local/CapCut/User Data/Projects/com.lveditor.draft"
CAPCUT_ROOT = Path("/mnt/c/Users/pedro/AppData/Local/CapCut/User Data/Projects/com.lveditor.draft")
DRAFT_NAME = "gongora_a_motor_parado_test_v2"
US = 1_000_000


# --------------------------------------------------------------------------- capas de texto
def layer_defs():
    """Mismos textos, posiciones y tiempos que overlay_text() del render, como capas estaticas."""
    T = rt.texts()
    end, tt, Y = rt.DUR, rt.text_times(), rt.FINAL_DY
    return [
        {"name": "01_titulo", "start": tt["title"][0], "end": tt["title"][1],
         "items": [("artist_s", 1150), ("title_s", 1225)], "text": "GÓNGORA / A MOTOR PARADO"},
        {"name": "02_titulo_final", "start": tt["final"], "end": end,
         "items": [("artist_l", 730 + Y), ("title_l", 832 + Y)], "text": "GÓNGORA / A MOTOR PARADO"},
        {"name": "03_disponible", "start": tt["avail"], "end": end, "line": 922 + Y,
         "items": [("avail", 1000 + Y), ("stores", 1062 + Y)], "text": "YA DISPONIBLE / Amazon Music · YouTube Music"},
        {"name": "04_spotify", "start": tt["soon"], "end": end,
         "items": [("soon", 1190 + Y)], "text": "YA DISPONIBLE EN SPOTIFY"},
    ], T


def render_layer(d, T):
    """RGBA 1080x1920: sombra suave + texto, sin fondo."""
    W, H = rt.W, rt.H
    col = np.zeros((H, W, 3), np.float32)
    alpha = np.zeros((H, W), np.float32)
    for key, cy in d["items"]:
        p = T[key]
        x0, y0 = int(W / 2 - p["w"] / 2), int(cy - p["h"] / 2)
        sl = (slice(y0, y0 + p["h"]), slice(x0, x0 + p["w"]))
        s = p["sh"] * 0.65
        # sombra negra y despues texto encima (composicion "over")
        a0 = alpha[sl]
        col[sl] *= (1 - s)[..., None]
        a0 = s + a0 * (1 - s)
        a = p["a"]
        col[sl] = col[sl] * (1 - a[..., None]) + p["col"] * a[..., None]
        alpha[sl] = a + a0 * (1 - a)
    if d.get("line"):
        ly = d["line"]
        alpha[ly:ly + 2, 540 - 70:540 + 70] = 0.8
        col[ly:ly + 2, 540 - 70:540 + 70] = 0.8
    # col esta premultiplicado por alpha -> desmultiplicar para PNG
    rgb = np.where(alpha[..., None] > 1e-4, col / np.maximum(alpha[..., None], 1e-4), 0)
    out = np.dstack([np.clip(rgb, 0, 1), alpha]) * 255 + 0.5
    return Image.fromarray(out.astype(np.uint8), "RGBA")


def cmd_layers():
    KIT.mkdir(parents=True, exist_ok=True)
    defs, T = layer_defs()
    layers = []
    for d in defs:
        f = KIT / f"text_{d['name']}.png"
        render_layer(d, T).save(f)
        layers.append({"file": f.name, "start_s": round(d["start"], 3), "end_s": round(d["end"], 3),
                       "fade_in_s": 0.6, "text": d["text"]})
        print("capa", f.name, f"{d['start']:.2f}-{d['end']:.2f}s")
    subprocess.run([FF, "-v", "error", "-y", "-i", str(AUDIO_SRC), "-c:a", "pcm_s16le", str(AUDIO)], check=True)
    MANIFEST.write_text(json.dumps({
        "song": "A motor parado", "artist": "GONGORA",
        "source_audio": "music/A_motor_parado_44100.flac", "excerpt": {"from": "00:49", "to": "01:13", "dur_s": 24.0},
        "canvas": {"w": rt.W, "h": rt.H, "fps": rt.FPS, "ratio": "9:16"},
        "plate": PLATE.name, "audio": AUDIO.name, "layers": layers,
        "bpm": round(60 / rt.PERIOD, 2),
    }, ensure_ascii=False, indent=1))
    print("manifiesto", MANIFEST)


# --------------------------------------------------------------------------- draft MP4 por ffmpeg
def cmd_ffmpeg():
    m = json.loads(MANIFEST.read_text())
    cmd = [FF, "-v", "error", "-y", "-i", str(PLATE)]
    for L in m["layers"]:
        cmd += ["-loop", "1", "-framerate", str(rt.FPS), "-t", f"{rt.DUR}", "-i", str(KIT / L["file"])]
    cmd += ["-i", str(AUDIO)]
    fc, prev = [], "0:v"
    for k, L in enumerate(m["layers"], 1):
        fc.append(f"[{k}:v]format=rgba,fade=t=in:st={L['start_s']}:d={L['fade_in_s']}:alpha=1,"
                  f"fade=t=out:st={max(L['end_s'] - 0.5, 0)}:d=0.5:alpha=1[t{k}]")
        nxt = f"v{k}"
        fc.append(f"[{prev}][t{k}]overlay=0:0:enable='between(t,{L['start_s']},{L['end_s']})'[{nxt}]")
        prev = nxt
    na = len(m["layers"]) + 1
    cmd += ["-filter_complex", ";".join(fc), "-map", f"[{prev}]", "-map", f"{na}:a",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-af", f"afade=t=out:st={rt.DUR - 0.4}:d=0.4", "-c:a", "aac", "-b:a", "320k",
            "-t", f"{rt.DUR}", "-movflags", "+faststart", str(DRAFT_MP4)]
    subprocess.run(cmd, check=True)
    print("draft", DRAFT_MP4)


# --------------------------------------------------------------------------- borrador de CapCut
def uid():
    return str(uuid.uuid4()).upper()


def tr(start_s, dur_s):
    return {"start": int(round(start_s * US)), "duration": int(round(dur_s * US))}


def video_material(path_win, kind, dur_s, w, h, name):
    return {
        "id": uid(), "type": kind, "path": path_win, "material_name": name, "duration": int(dur_s * US),
        "width": w, "height": h, "category_name": "local", "category_id": "", "check_flag": 62978047,
        "local_material_id": "", "material_id": "", "media_path": "", "has_audio": kind == "video",
        "crop": {"upper_left_x": 0.0, "upper_left_y": 0.0, "upper_right_x": 1.0, "upper_right_y": 0.0,
                 "lower_left_x": 0.0, "lower_left_y": 1.0, "lower_right_x": 1.0, "lower_right_y": 1.0},
        "crop_ratio": "free", "crop_scale": 1.0, "audio_fade": None, "extra_type_option": 0,
        "is_unified_beauty_mode": False, "source": 0, "source_platform": 0,
    }


def audio_material(path_win, dur_s, name):
    return {
        "id": uid(), "type": "extract_music", "path": path_win, "name": name, "duration": int(dur_s * US),
        "category_name": "local", "category_id": "", "check_flag": 1, "local_material_id": uid().lower(),
        "music_id": uid().lower(), "app_id": 0, "copyright_limit_type": "none", "effect_id": "",
        "formula_id": "", "intensifies_path": "", "is_ai_clone_tone": False, "is_text_edit_overdub": False,
        "is_ugc": False, "query": "", "request_id": "", "resource_id": "", "search_id": "", "source_from": "",
        "source_platform": 0, "team_id": "", "text_id": "", "tone_category_id": "", "tone_category_name": "",
        "tone_effect_id": "", "tone_effect_name": "", "tone_platform": "", "tone_second_category_id": "",
        "tone_second_category_name": "", "tone_speaker": "", "tone_type": "", "video_id": "", "wave_points": [],
    }


def segment(mat_id, target, source, refs, render_index, visual=True, volume=1.0):
    s = {
        "id": uid(), "material_id": mat_id, "target_timerange": target, "source_timerange": source,
        "speed": 1.0, "volume": volume, "last_nonzero_volume": 1.0, "visible": True, "reverse": False,
        "extra_material_refs": refs, "common_keyframes": [], "keyframe_refs": [],
        "render_index": render_index, "track_render_index": 0, "track_attribute": 0,
        "enable_adjust": visual, "enable_color_curves": True, "enable_color_wheels": True, "enable_lut": visual,
        "enable_color_correct_adjust": False, "enable_color_match_adjust": False,
        "enable_smart_color_adjust": False, "intensifies_audio": False, "is_placeholder": False,
        "is_tone_modify": False, "cartoon": False, "template_id": "", "template_scene": "default",
        "uniform_scale": {"on": True, "value": 1.0} if visual else None,
        "clip": {"alpha": 1.0, "flip": {"horizontal": False, "vertical": False}, "rotation": 0.0,
                 "scale": {"x": 1.0, "y": 1.0}, "transform": {"x": 0.0, "y": 0.0}} if visual else None,
        "hdr_settings": {"intensity": 1.0, "mode": 1, "nits": 1000} if visual else None,
    }
    return s


def track(kind, segs, attribute=0):
    return {"id": uid(), "type": kind, "attribute": attribute, "flag": 0, "is_default_name": True,
            "name": "", "segments": segs}


def cmd_capcut():
    m = json.loads(MANIFEST.read_text())
    tmpl_dir = next(p for p in CAPCUT_ROOT.iterdir() if (p / "draft_content.json").exists()
                    and p.name != DRAFT_NAME)
    content = json.loads((tmpl_dir / "draft_content.json").read_text(encoding="utf-8"))
    meta = json.loads((tmpl_dir / "draft_meta_info.json").read_text(encoding="utf-8"))

    ddir = CAPCUT_ROOT / DRAFT_NAME
    if ddir.exists():
        sys.exit(f"ya existe {ddir}; borralo a mano si quieres regenerarlo")
    media = ddir / "gongora_media"
    media.mkdir(parents=True)
    for f in [PLATE, AUDIO] + [KIT / L["file"] for L in m["layers"]]:
        shutil.copy2(f, media / f.name)
    win = f"{CAPCUT_ROOT_WIN}/{DRAFT_NAME}/gongora_media/"
    dur = rt.DUR

    mats = content["materials"]
    tracks = []
    # pista principal: placa de video (sin audio propio)
    plate = video_material(win + PLATE.name, "video", dur, rt.W, rt.H, PLATE.name)
    sp = {"id": uid(), "type": "speed", "mode": 0, "speed": 1.0, "curve_speed": None}
    mats["videos"].append(plate)
    mats["speeds"].append(sp)
    tracks.append(track("video", [segment(plate["id"], tr(0, dur), tr(0, dur), [sp["id"]], 0, volume=0.0)]))
    # superposiciones: una pista por capa de texto
    for k, L in enumerate(m["layers"], 1):
        img = video_material(win + L["file"], "photo", 10800.0, rt.W, rt.H, L["file"])
        sp = {"id": uid(), "type": "speed", "mode": 0, "speed": 1.0, "curve_speed": None}
        mats["videos"].append(img)
        mats["speeds"].append(sp)
        d = L["end_s"] - L["start_s"]
        tracks.append(track("video", [segment(img["id"], tr(L["start_s"], d), tr(0, d), [sp["id"]], k)],
                            attribute=0))
    # audio
    au = audio_material(win + AUDIO.name, dur, AUDIO.name)
    sp = {"id": uid(), "type": "speed", "mode": 0, "speed": 1.0, "curve_speed": None}
    mats["audios"].append(au)
    mats["speeds"].append(sp)
    tracks.append(track("audio", [segment(au["id"], tr(0, dur), tr(0, dur), [sp["id"]], 0, visual=False)]))

    now_us = int(time.time() * US)
    content.update({
        "id": uid(), "name": DRAFT_NAME, "duration": int(dur * US), "fps": float(rt.FPS),
        "create_time": now_us // US, "update_time": now_us // US, "tracks": tracks,
        "canvas_config": {"ratio": "9:16", "width": rt.W, "height": rt.H, "background": None},
    })
    tl_id = content["id"]
    body = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    (ddir / "draft_content.json").write_text(body, encoding="utf-8")
    (ddir / "Timelines" / tl_id).mkdir(parents=True)
    (ddir / "Timelines" / tl_id / "draft_content.json").write_text(body, encoding="utf-8")
    (ddir / "Timelines" / "project.json").write_text(json.dumps({
        "config": {"color_space": -1, "hdr_vivid": False, "mixed_track_mode_on": False,
                   "render_index_track_mode_on": False, "use_float_render": False},
        "create_time": now_us, "id": uid(), "main_timeline_id": tl_id,
        "timelines": [{"create_time": now_us, "id": tl_id, "is_marked_delete": False,
                       "name": "Línea de tiempo 01", "update_time": now_us}],
        "update_time": now_us, "version": 0}, ensure_ascii=False), encoding="utf-8")
    (ddir / "timeline_layout.json").write_text(json.dumps({
        "dockItems": [{"dockIndex": 0, "ratio": 1, "timelineIds": [tl_id],
                       "timelineNames": ["Línea de tiempo 01"]}], "layoutOrientation": 1},
        ensure_ascii=False), encoding="utf-8")
    shutil.copy2(tmpl_dir / "draft_agency_config.json", ddir / "draft_agency_config.json")
    (ddir / "draft_settings").write_text(
        f"[General]\r\ndraft_create_time={now_us // US}\r\ndraft_last_edit_time={now_us // US}\r\n"
        "real_edit_seconds=0\r\nreal_edit_keys=0\r\n", encoding="utf-8")
    draft_id = uid()
    meta.update({
        "draft_fold_path": f"{CAPCUT_ROOT_WIN}/{DRAFT_NAME}", "draft_id": draft_id, "draft_name": DRAFT_NAME,
        "draft_cover": "", "tm_draft_create": now_us, "tm_draft_modified": now_us, "tm_duration": int(dur * US),
        "draft_timeline_materials_size_": sum(f.stat().st_size for f in media.iterdir()),
    })
    (ddir / "draft_meta_info.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    print("borrador", ddir)


if __name__ == "__main__":
    {"layers": cmd_layers, "ffmpeg": cmd_ffmpeg, "capcut": cmd_capcut}[sys.argv[1]]()
