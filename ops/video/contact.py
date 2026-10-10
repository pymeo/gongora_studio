import sys; from pathlib import Path; from PIL import Image
fs = sorted(Path(sys.argv[1]).glob("*.jpg")); cols = 5; tw, th = 324, 576
rows = (len(fs) + cols - 1) // cols
sheet = Image.new("RGB", (cols * tw, rows * th), (40, 40, 40))
for k, f in enumerate(fs): sheet.paste(Image.open(f).resize((tw, th)), ((k % cols) * tw, (k // cols) * th))
sheet.save(sys.argv[2], quality=88)
