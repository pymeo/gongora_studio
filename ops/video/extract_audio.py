"""Extrae 00:49-01:13 de A motor parado con precision de muestra (sin recodificar con perdida)."""
import subprocess, sys, numpy as np, imageio_ffmpeg
from pathlib import Path

FF = imageio_ffmpeg.get_ffmpeg_exe()
SRC = Path("music/A_motor_parado_44100.flac")
OUT = Path("output/social/a_motor_parado/work")
SR, START, END = 44100, 49.0, 73.0

raw = subprocess.run([FF, "-v", "error", "-i", str(SRC), "-f", "s16le", "-acodec", "pcm_s16le",
                      "-ac", "2", "-ar", str(SR), "-"], capture_output=True, check=True).stdout
pcm = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2)
clip = pcm[int(START * SR):int(END * SR)]
print("total_s", len(pcm) / SR, "clip_samples", len(clip), "clip_s", len(clip) / SR)
OUT.mkdir(parents=True, exist_ok=True)
np.save(OUT / "clip_pcm.npy", clip)
subprocess.run([FF, "-v", "error", "-y", "-f", "s16le", "-ar", str(SR), "-ac", "2", "-i", "-",
                "-c:a", "flac", str(OUT / "a_motor_parado_0049_0113.flac")], input=clip.tobytes(), check=True)
