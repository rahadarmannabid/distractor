"""Images the instructor put in a problem (statement, test cases, constraints), for the AI prompts.

The rich editor stores them as <img src="/quiz-images/{asset_id}/{filename}"> (see instructor.py
upload-survey-image). The prompt text replaces each one with an "[Image N]" marker, and the images
themselves are sent to the model as data URLs in the same order.
"""

import base64
import io
import logging
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

QUIZ_ASSETS_DIR = Path("surveys") / "quiz_assets"
MAX_IMAGES = 6
# Longest side sent to the model; larger images cost more tokens without helping it read a diagram.
MAX_SIDE = 1568
_MAX_SOURCE_BYTES = 5 * 1024 * 1024

IMG_TAG_RE = re.compile(r"(?is)<img\b[^>]*>")
_SRC_RE = re.compile(r"""(?is)\bsrc\s*=\s*["']?([^"'\s>]+)""")
_QUIZ_IMAGE_SRC_RE = re.compile(r"^/quiz-images/([a-zA-Z0-9_-]{8,128})/([A-Za-z0-9._-]{1,200})$")
# What OpenAI vision accepts as is; anything else is converted to PNG (needs Pillow).
_DIRECT_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

_cache: Dict[Tuple[str, float], Optional[str]] = {}
_cache_lock = threading.Lock()


def image_path(src: str) -> Optional[Path]:
    """The file behind a /quiz-images/... src, or None if it is not one of ours."""
    m = _QUIZ_IMAGE_SRC_RE.match((src or "").strip())
    if not m:
        return None
    path = (QUIZ_ASSETS_DIR / m.group(1) / m.group(2)).resolve()
    if not str(path).startswith(str(QUIZ_ASSETS_DIR.resolve())) or not path.is_file():
        return None
    return path


def img_src(tag: str) -> str:
    m = _SRC_RE.search(tag)
    return m.group(1) if m else ""


def _encode(path: Path) -> Optional[str]:
    raw = path.read_bytes()
    if len(raw) > _MAX_SOURCE_BYTES:
        return None
    ext = path.suffix.lower()
    try:
        from PIL import Image
    except ImportError:
        mime = _DIRECT_MIME.get(ext)
        return f"data:{mime};base64,{base64.b64encode(raw).decode()}" if mime else None
    if ext == ".svg":
        return None  # Pillow cannot rasterize SVG
    with Image.open(io.BytesIO(raw)) as im:
        im.seek(0)  # first frame of an animated GIF
        if max(im.size) <= MAX_SIDE and ext in _DIRECT_MIME:
            return f"data:{_DIRECT_MIME[ext]};base64,{base64.b64encode(raw).decode()}"
        im = im.convert("RGBA") if im.mode in ("P", "LA", "RGBA") else im.convert("RGB")
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"


def data_url(src: str) -> Optional[str]:
    """The image at a /quiz-images/... src as a data URL for the model (cached by file and mtime)."""
    path = image_path(src)
    if path is None:
        return None
    key = (str(path), path.stat().st_mtime)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    try:
        url = _encode(path)
    except Exception as e:
        logger.warning("Problem image %s not encoded: %s", path, e)
        url = None
    with _cache_lock:
        _cache[key] = url
    return url
