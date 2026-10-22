#!/usr/bin/env python3
"""Generate the Itraji Cliper app icons from the master logo mark.

Source of truth: logo-mark.png in this folder (the white 3D "IC" cube
mark the owner supplied 2026-10-22, cropped square from his full
lockup). Produces root icon-512.png / icon-192.png and a multi-size
icon.ico; the same mark ships in ui/icon-*.png as the favicon /
in-app logo. ClipMakerPC.spec embeds the .ico as base64 - after
running this, re-embed icon.ico into the spec's _ICO_B64.
"""
from PIL import Image
import os

outdir = os.path.dirname(os.path.abspath(__file__))
master = Image.open(os.path.join(outdir, "logo-mark.png")).convert("RGB")

master.resize((512, 512), Image.LANCZOS).save(os.path.join(outdir, "icon-512.png"))
master.resize((192, 192), Image.LANCZOS).save(os.path.join(outdir, "icon-192.png"))
sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
master.resize((256, 256), Image.LANCZOS).save(
    os.path.join(outdir, "icon.ico"), format="ICO",
    sizes=sizes)
print("Itraji Cliper icon.ico + PNGs written from logo-mark.png")
