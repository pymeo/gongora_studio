"""Analisis simple de energia, onsets y tempo del clip para sincronizar el montaje."""
import json, numpy as np
from pathlib import Path
W = Path("output/social/a_motor_parado/work")
SR = 44100
x = np.load(W / "clip_pcm.npy").astype(np.float32).mean(1) / 32768
hop, win = 512, 2048
n = (len(x) - win) // hop
frames = np.lib.stride_tricks.as_strided(x, (n, win), (x.strides[0] * hop, x.strides[0]))
spec = np.abs(np.fft.rfft(frames * np.hanning(win), axis=1))
rms = np.sqrt((frames ** 2).mean(1))
logs = np.log1p(spec * 10)
flux = np.maximum(0, np.diff(logs, axis=0)).sum(1); flux = np.r_[0, flux]
flux = (flux - flux.mean()) / flux.std()
t = np.arange(n) * hop / SR
# tempo via autocorrelacion del flux
ac = np.correlate(flux, flux, "full")[n - 1:]
lags = np.arange(len(ac)) * hop / SR
m = (lags > 60 / 180) & (lags < 60 / 70)
period = lags[m][np.argmax(ac[m])]
bpm = 60 / period
# fase: maximizar flux sobre rejilla
best = max(np.arange(0, period, 0.005), key=lambda p: sum(np.interp(np.arange(p, 24, period), t, flux)))
beats = np.arange(best, 24, period)
# energia por segundo
sec = [float(rms[(t >= s) & (t < s + 1)].mean()) for s in range(24)]
peaks = [float(t[i]) for i in range(1, n - 1) if flux[i] > 2.5 and flux[i] == flux[max(0, i - 8):i + 8].max()]
json.dump({"bpm": bpm, "period": period, "phase": best, "beats": beats.tolist(), "rms_per_sec": sec, "strong_onsets": peaks},
          open(W / "analysis.json", "w"), indent=1)
print("bpm %.2f period %.4f phase %.3f" % (bpm, period, best))
print("rms/s", " ".join("%.3f" % v for v in sec))
print("strong onsets", " ".join("%.2f" % p for p in peaks))
