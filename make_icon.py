#!/usr/bin/env python3
"""Generate the ClipMaker PC app icon matching the Clip Maker web app icon:
lime (#C8FF2E) clapperboard on solid black - rounded body, angled open slat
with 5 black diagonal stripes, 4 black stripes on the body top edge, one
solid black play triangle centered on the body, no lettering."""
from PIL import Image, ImageDraw
import os

LIME = (200, 255, 46)
BLACK = (0, 0, 0)
SIZE = 512

def draw_icon(size):
    img = Image.new("RGB", (size, size), BLACK)
    s = size / 512.0

    # ---- slat (own layer, rotated) ----
    slat_w, slat_h = int(360 * s), int(84 * s)
    slat = Image.new("RGBA", (slat_w + 40, slat_h + 40), (0, 0, 0, 0))
    sd = ImageDraw.Draw(slat)
    sd.rounded_rectangle([20, 20, 20 + slat_w, 20 + slat_h], radius=int(18 * s), fill=LIME + (255,))
    # 5 black diagonal stripes on the slat
    for i in range(5):
        x0 = 52 * s + i * 68 * s
        sd.polygon([(x0, 20 + slat_h), (x0 + 30 * s, 20 + slat_h),
                    (x0 + 30 * s + 34 * s, 20), (x0 + 34 * s, 20)], fill=BLACK + (255,))
    # clip stripes to the slat shape
    mask = Image.new("L", slat.size, 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([20, 20, 20 + slat_w, 20 + slat_h], radius=int(18 * s), fill=255)
    slat.putalpha(mask)
    slat = slat.rotate(13, expand=True, resample=Image.BICUBIC)
    img.paste(slat, (int(66 * s), int(58 * s)), slat)

    d = ImageDraw.Draw(img)
    # ---- body ----
    bx0, by0, bx1, by1 = 76 * s, 218 * s, 436 * s, 448 * s
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=int(28 * s), fill=LIME)
    # 4 black diagonal stripes along the body's top edge (clipped to body)
    stripe_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sld = ImageDraw.Draw(stripe_layer)
    for i in range(4):
        x0 = 116 * s + i * 82 * s
        sld.polygon([(x0, by0 + 40 * s), (x0 + 34 * s, by0 + 40 * s),
                     (x0 + 34 * s + 20 * s, by0), (x0 + 20 * s, by0)], fill=BLACK + (255,))
    body_mask = Image.new("L", (size, size), 0)
    bmd = ImageDraw.Draw(body_mask)
    bmd.rounded_rectangle([bx0, by0, bx1, by1], radius=int(28 * s), fill=255)
    img.paste(stripe_layer, (0, 0), Image.composite(stripe_layer, Image.new("RGBA", (size, size), (0, 0, 0, 0)), body_mask))
    # redraw after paste (paste replaced region) - paste with mask only writes stripe pixels
    d = ImageDraw.Draw(img)
    # ---- black play triangle centered on body ----
    cx, cy = 256 * s, 348 * s
    tw, th = 118 * s, 138 * s
    d.polygon([(cx - tw / 2, cy - th / 2), (cx + tw / 2, cy), (cx - tw / 2, cy + th / 2)], fill=BLACK)
    return img

outdir = os.path.dirname(os.path.abspath(__file__))
master = draw_icon(512)
master.save(os.path.join(outdir, "icon-512.png"))
master.resize((192, 192), Image.LANCZOS).save(os.path.join(outdir, "icon-192.png"))
sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
icos = [master.resize(sz, Image.LANCZOS) for sz in sizes]
icos[0].save(os.path.join(outdir, "icon.ico"), sizes=[(sz[0], sz[1]) for sz in sizes])
print("clapperboard icon.ico + PNGs written")
