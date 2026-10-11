# -*- coding: utf-8 -*-
"""Generate the app icon (battery + lightning bolt) as a multi-size .ico."""
from PIL import Image, ImageDraw

S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

BG = (37, 99, 235, 255)      # blue
WHITE = (255, 255, 255, 255)
GREEN = (52, 199, 123, 255)
YELLOW = (255, 208, 60, 255)

pad = int(S * 0.045)
d.rounded_rectangle([pad, pad, S - pad, S - pad], radius=int(S * 0.20), fill=BG)

# battery body (vertical)
bx0, by0 = int(S * 0.30), int(S * 0.24)
bx1, by1 = int(S * 0.70), int(S * 0.80)
d.rounded_rectangle([bx0, by0, bx1, by1], radius=int(S * 0.06),
                    outline=WHITE, width=int(S * 0.036))

# battery terminal (top nub)
nw = int(S * 0.12)
d.rounded_rectangle([S // 2 - nw // 2, by0 - int(S * 0.075),
                     S // 2 + nw // 2, by0 + int(S * 0.01)],
                    radius=int(S * 0.02), fill=WHITE)

# charge fill (bottom part)
inset = int(S * 0.045)
d.rounded_rectangle([bx0 + inset, by0 + int(S * 0.20),
                     bx1 - inset, by1 - inset],
                    radius=int(S * 0.03), fill=GREEN)

# lightning bolt
cx = S // 2
bolt = [
    (cx + int(S * 0.045), int(S * 0.31)),
    (cx - int(S * 0.105), int(S * 0.51)),
    (cx + int(S * 0.005), int(S * 0.51)),
    (cx - int(S * 0.055), int(S * 0.72)),
    (cx + int(S * 0.110), int(S * 0.465)),
    (cx - int(S * 0.005), int(S * 0.465)),
]
d.polygon(bolt, fill=YELLOW)

img.save(r"C:\Users\Administrator\AppData\Local\MechrevoChargeLimit\battery_limit.ico",
         sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.resize((256, 256), Image.LANCZOS).save(r"C:\Users\Administrator\AppData\Local\Temp\tcc-i18n\icon_preview.png")
print("icon written")
