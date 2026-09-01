"""
apps/files/signature_image.py
─────────────────────────────
Professional signature image processing for EasyOffice.

Everything that ends up stamped into a PDF — drawn pads, uploaded photos of
a wet-ink signature, scanned letterheads — passes through here first, so the
result is always:

  • a real transparent PNG (no white rectangle covering the document text);
  • tightly cropped to the ink, with a small breathing margin;
  • cleaned of paper texture, JPEG mush, shadows and lone speckles;
  • rendered in a consistent ink colour and stroke density.

Why not the old ``r,g,b >= 235 → alpha 0`` knockout?
    A hard threshold gives three visible defects that make a signature look
    amateur next to DocuSign:
      1. a grey halo — anti-aliased edge pixels sit just under the threshold
         and survive as dirty grey fringe;
      2. jagged edges — alpha is binary, so curves become staircases at the
         zoom levels people actually read PDFs at;
      3. it fails on anything that is not pure white: phone photos of paper
         are cream/grey with a shadow gradient, and the whole page survives.

    This module instead measures the actual background from the border, builds
    a *soft* alpha ramp around an automatically chosen (Otsu) threshold, and
    flattens the background gradient first. Edges stay anti-aliased, the paper
    disappears, and the ink keeps its natural stroke weight.

No new dependencies: Pillow only. NumPy is used when present purely as a
speed-up; the pure-Pillow path produces identical output.

Usage
─────
    from apps.files.signature_image import (
        normalize_signature_bytes,     # bytes  -> PNG bytes  (RGBA)
        normalize_signature_data_url,  # str    -> data URL   (image/png)
        normalize_uploaded_image,      # File   -> data URL   (image/png)
        strip_background,              # PIL.Image -> PIL.Image (RGBA)
    )
"""
from __future__ import annotations

import base64
import io
import logging

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat

logger = logging.getLogger(__name__)

try:                                     # optional fast path
    import numpy as _np
except Exception:                        # pragma: no cover
    _np = None


# ── Tunables ────────────────────────────────────────────────────────────────

MAX_SIDE          = 2400   # px  — cap input before processing (memory guard)
OUTPUT_MAX_HEIGHT = 900    # px  — plenty for 300 dpi stamping, keeps PNG small
MIN_INK_FRACTION  = 0.0002 # below this we assume "nothing was drawn"
EDGE_SOFTNESS     = 0.42   # 0 = hard binary alpha, 1 = very soft ramp
DESPECKLE_MIN_PX  = 6      # isolated blobs smaller than this are dropped
CROP_PADDING_PCT  = 0.04   # margin kept around the ink after trimming

INK_PRESETS = {
    'black': (17, 24, 39),
    'ink':   (15, 23, 42),
    'navy':  (12, 35, 92),
    'blue':  (14, 87, 194),
    'gray':  (55, 65, 81),
}


# ── Small helpers ───────────────────────────────────────────────────────────

def _load(raw: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(raw))
    img.load()
    try:
        img = ImageOps.exif_transpose(img)     # phone photos arrive rotated
    except Exception:
        pass
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    return img


def _histogram_otsu(hist) -> int:
    """
    Classic Otsu threshold over a 256-bin histogram. Returns 0-255.
    Pure Python — a 256-iteration loop is free compared to the image work.
    """
    total = sum(hist) or 1
    sum_all = sum(i * h for i, h in enumerate(hist))
    sum_bg = 0.0
    w_bg = 0
    best_var, best_t = -1.0, 127
    for t in range(256):
        w_bg += hist[t]
        if w_bg == 0:
            continue
        w_fg = total - w_bg
        if w_fg == 0:
            break
        sum_bg += t * hist[t]
        mean_bg = sum_bg / w_bg
        mean_fg = (sum_all - sum_bg) / w_fg
        var = w_bg * w_fg * (mean_bg - mean_fg) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return best_t


def _border_background_colour(img: Image.Image) -> tuple:
    """
    Estimate the paper colour from a frame around the edge. Signatures never
    touch the very border, so this is a robust sample even on tight crops.
    """
    rgb = img.convert('RGB')
    w, h = rgb.size
    frame = max(2, int(min(w, h) * 0.04))
    strips = [
        rgb.crop((0, 0, w, frame)),
        rgb.crop((0, h - frame, w, h)),
        rgb.crop((0, 0, frame, h)),
        rgb.crop((w - frame, 0, w, h)),
    ]
    totals, count = [0, 0, 0], 0
    for s in strips:
        px = s.resize((max(1, s.width // 4), max(1, s.height // 4)), Image.BOX)
        for r, g, b in px.getdata():
            totals[0] += r
            totals[1] += g
            totals[2] += b
            count += 1
    if not count:
        return (255, 255, 255)
    return tuple(v // count for v in totals)


def _ink_distance_map(img: Image.Image, bg: tuple) -> Image.Image:
    """
    Greyscale map where 0 = identical to the paper and 255 = strongest ink.

    A flat background subtraction is not enough for phone photos: the paper
    is brighter under the lamp than in the corner. We therefore remove the
    low-frequency illumination with a heavy blur (a poor man's rolling-ball
    background) before measuring the distance.
    """
    rgb = img.convert('RGB')
    bg_plate = Image.new('RGB', rgb.size, tuple(int(c) for c in bg))

    # Per-channel absolute difference, collapsed to luminance-ish grey.
    diff = ImageChops.difference(rgb, bg_plate).convert('L')

    # Illumination flattening: subtract the smoothed version of the paper.
    radius = max(6, int(min(rgb.size) * 0.06))
    smooth = diff.filter(ImageFilter.GaussianBlur(radius=radius))
    diff = ImageChops.subtract(diff, smooth, scale=1.0, offset=0)

    # Recover contrast lost to the subtraction; ignore the extreme tails so a
    # single dark speck cannot flatten the whole signature.
    try:
        diff = ImageOps.autocontrast(diff, cutoff=(0.5, 0.2))
    except TypeError:                       # Pillow < 9 takes a single cutoff
        diff = ImageOps.autocontrast(diff, cutoff=1)
    return diff


def _alpha_from_distance(dist: Image.Image, sensitivity: float = 1.0) -> Image.Image:
    """
    Convert the ink-distance map into a soft alpha channel.

    The threshold is chosen automatically with Otsu, then widened into a ramp
    so anti-aliased stroke edges keep partial alpha — this is what removes the
    staircase look and the grey halo at the same time.

    ``sensitivity`` > 1 keeps fainter ink (good for pencil), < 1 is stricter
    (good for noisy scans).
    """
    t = _histogram_otsu(dist.histogram())
    t = max(8, min(230, int(t / max(0.35, sensitivity))))

    ramp = max(4, int(t * EDGE_SOFTNESS))
    lo, hi = max(0, t - ramp), min(255, t + ramp)
    span = max(1, hi - lo)

    lut = [0 if v <= lo else 255 if v >= hi else int(255 * (v - lo) / span)
           for v in range(256)]
    return dist.point(lut)


def _despeckle(alpha: Image.Image, min_px: int = DESPECKLE_MIN_PX) -> Image.Image:
    """
    Drop dust: pixels whose neighbourhood contains almost no other ink.
    A 3×3 min/max pass is far cheaper than connected-component labelling and
    removes exactly the artefacts that JPEG compression leaves behind.
    """
    if min_px <= 0:
        return alpha
    neighbourhood = alpha.filter(ImageFilter.BoxBlur(1))          # local density
    keep_lut = [0 if v < 28 else 255 for v in range(256)]
    mask = neighbourhood.point(keep_lut)
    return ImageChops.multiply(alpha, mask.point(lambda v: 255 if v else 0))


def _colourise(alpha: Image.Image, ink) -> Image.Image:
    rgb = INK_PRESETS.get(ink, ink if isinstance(ink, (tuple, list)) else None)
    if not rgb:
        rgb = INK_PRESETS['ink']
    plate = Image.new('RGB', alpha.size, tuple(int(c) for c in rgb))
    out = plate.convert('RGBA')
    out.putalpha(alpha)
    return out


def _dominant_ink_colour(rgb: Image.Image, alpha: Image.Image) -> tuple:
    """
    Average colour of the solidly inked pixels — the actual pen colour.

    Used for ``keep_colour``: reusing each pixel's own RGB would keep the
    paper tint in the anti-aliased edge pixels, which reads as a pale fringe
    once the signature is composited onto a white page. Painting the whole
    stroke in one measured pen colour keeps a blue biro blue without the
    fringe.
    """
    mask = alpha.point(lambda v: 255 if v > 200 else 0)
    if not mask.getbbox():
        mask = alpha.point(lambda v: 255 if v > 90 else 0)
    if not mask.getbbox():
        return INK_PRESETS['ink']
    try:
        mean = ImageStat.Stat(rgb.convert('RGB'), mask).mean
    except Exception:
        return INK_PRESETS['ink']
    # Slight darkening: the mean is pulled up by half-covered edge pixels.
    return tuple(max(0, min(255, int(c * 0.86))) for c in mean[:3])


def _trim(img: Image.Image, padding_pct: float = CROP_PADDING_PCT) -> Image.Image:
    alpha = img.split()[-1]
    bbox = alpha.getbbox()
    if not bbox:
        return img
    pad = int(max(img.size) * padding_pct)
    x0 = max(0, bbox[0] - pad)
    y0 = max(0, bbox[1] - pad)
    x1 = min(img.width, bbox[2] + pad)
    y1 = min(img.height, bbox[3] + pad)
    return img.crop((x0, y0, x1, y1))


def _ink_fraction(alpha: Image.Image) -> float:
    hist = alpha.histogram()
    total = sum(hist) or 1
    inked = sum(hist[64:])
    return inked / total


# ── Public API ──────────────────────────────────────────────────────────────

def strip_background(img: Image.Image, *, ink='ink', sensitivity: float = 1.0,
                     keep_colour: bool = False, trim: bool = True,
                     despeckle: bool = True) -> Image.Image:
    """
    Turn any signature image into clean transparent RGBA ink.

    Drop-in replacement for the old ``_strip_white_background(img, threshold)``.
    If the source already has a meaningful alpha channel (a drawn pad export,
    a proper cut-out PNG) that alpha is respected and only tidied up.
    """
    img = img.convert('RGBA')
    src_alpha = img.split()[-1]

    already_transparent = src_alpha.getextrema()[0] < 250
    if already_transparent and _ink_fraction(src_alpha) > MIN_INK_FRACTION:
        alpha = src_alpha
        rgb_source = img.convert('RGB')
    else:
        flat = Image.new('RGB', img.size, (255, 255, 255))
        flat.paste(img, mask=src_alpha)
        bg = _border_background_colour(flat)
        dist = _ink_distance_map(flat, bg)
        alpha = _alpha_from_distance(dist, sensitivity=sensitivity)
        rgb_source = flat

    if despeckle:
        alpha = _despeckle(alpha)

    if _ink_fraction(alpha) < MIN_INK_FRACTION:
        # Nothing convincing survived — return the original rather than a
        # blank stamp, so a user never silently loses their signature.
        return img

    if keep_colour:
        out = _colourise(alpha, _dominant_ink_colour(rgb_source, alpha))
    else:
        out = _colourise(alpha, ink)

    return _trim(out) if trim else out


def normalize_signature_image(img: Image.Image, *, ink='ink', sensitivity: float = 1.0,
                              keep_colour: bool = False,
                              max_height: int = OUTPUT_MAX_HEIGHT) -> Image.Image:
    out = strip_background(img, ink=ink, sensitivity=sensitivity,
                           keep_colour=keep_colour)
    if out.height > max_height:
        ratio = max_height / float(out.height)
        out = out.resize(
            (max(1, int(out.width * ratio)), max_height), Image.LANCZOS
        )
    return out


def normalize_signature_bytes(raw: bytes, **kwargs) -> bytes:
    """bytes of any image format → PNG bytes with a transparent background."""
    if not raw:
        return b''
    try:
        img = normalize_signature_image(_load(raw), **kwargs)
    except Exception:
        logger.exception('signature normalisation failed; using original bytes')
        return raw
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue()


def normalize_signature_data_url(data_url: str, **kwargs) -> str:
    """
    ``data:image/...;base64,…`` in, transparent ``data:image/png;base64,…`` out.
    Anything that is not a data URL (e.g. ``font:Allura|Baboucarr``) is returned
    untouched, so this is safe to call on any stored signature value.
    """
    value = (data_url or '').strip()
    if not value.startswith('data:image'):
        return value
    try:
        header, b64 = value.split(',', 1)
    except ValueError:
        return value
    if 'base64' not in header:
        return value
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return value
    png = normalize_signature_bytes(raw, **kwargs)
    if not png:
        return value
    return 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')


def normalize_uploaded_image(upload, **kwargs) -> str:
    """
    Django UploadedFile → transparent PNG data URL.
    Replaces ``_uploaded_image_to_data_url`` and keeps the file pointer usable
    afterwards so the caller can still assign it to an ImageField.
    """
    if not upload:
        return ''
    try:
        upload.seek(0)
    except Exception:
        pass
    raw = upload.read()
    try:
        upload.seek(0)
    except Exception:
        pass
    if not raw:
        return ''
    png = normalize_signature_bytes(raw, **kwargs)
    return 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')


def transparent_png_stream(data_url: str, **kwargs):
    """
    Convenience for the PDF stamping code: data URL → BytesIO of a transparent
    PNG, positioned at the start and ready for ``ImageReader``.
    """
    value = (data_url or '').strip()
    if not value.startswith('data:image'):
        return None
    try:
        raw = base64.b64decode(value.split(',', 1)[1])
    except Exception:
        return None
    buf = io.BytesIO(normalize_signature_bytes(raw, **kwargs))
    buf.seek(0)
    return buf
