"""Post-processing watermark insertion into PPTX slides."""
from __future__ import annotations

from pptx import Presentation
from pptx.util import Inches, Emu
from PIL import Image


SLIDE_W = Emu(Inches(13.33))
SLIDE_H = Emu(Inches(7.5))

BR_MAX_W = Emu(Inches(2.5))
BR_MAX_H = Emu(Inches(0.5))
BR_MARGIN_RIGHT = Emu(Inches(0.5))
BR_MARGIN_BOTTOM = Emu(Inches(0.3))

CT_MAX_W = Emu(Inches(6.0))
CT_MAX_H = Emu(Inches(0.7))
CT_Y = Emu(Inches(0.3))


def _image_size(image_path: str) -> tuple[int, int]:
    with Image.open(image_path) as img:
        return img.size


def _fit(img_w: int, img_h: int, max_w: int, max_h: int) -> tuple[Emu, Emu]:
    ratio_w = max_w / img_w
    ratio_h = max_h / img_h
    scale = min(ratio_w, ratio_h)
    return Emu(int(img_w * scale)), Emu(int(img_h * scale))


def apply_watermark(pptx_path: str, watermark_image_path: str, mode: str):
    if mode not in ("bottom_right", "title_and_bottom_right"):
        return

    prs = Presentation(pptx_path)
    img_w, img_h = _image_size(watermark_image_path)

    br_w, br_h = _fit(img_w, img_h, BR_MAX_W, BR_MAX_H)
    br_left = Emu(SLIDE_W - BR_MARGIN_RIGHT - br_w)
    br_top = Emu(SLIDE_H - BR_MARGIN_BOTTOM - br_h)

    for i, slide in enumerate(prs.slides):
        if mode == "title_and_bottom_right" and i == 0:
            ct_w, ct_h = _fit(img_w, img_h, CT_MAX_W, CT_MAX_H)
            ct_left = Emu((SLIDE_W - ct_w) // 2)
            slide.shapes.add_picture(watermark_image_path, ct_left, CT_Y, ct_w, ct_h)
        else:
            slide.shapes.add_picture(watermark_image_path, br_left, br_top, br_w, br_h)

    prs.save(pptx_path)
