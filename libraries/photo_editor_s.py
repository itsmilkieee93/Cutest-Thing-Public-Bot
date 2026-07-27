"""
photo_editor.py

Pillow-based image editing toolkit — filters, color temperature,
resolution/resize, brightness/contrast/saturation/sharpness.

Usage as a module:
    from photo_editor import PhotoEditor

    editor = PhotoEditor("input.jpg")
    editor.apply_filter("sepia") \
          .adjust_temperature(30) \
          .resize(1080, 1080) \
          .adjust_brightness(1.1) \
          .save("output.jpg")

Usage as CLI:
    python photo_editor.py input.jpg output.jpg --filter sepia --temp 30 --width 1080 --height 1080
"""

import argparse
from io import BytesIO
from PIL import Image, ImageFilter, ImageEnhance, ImageOps


class PhotoEditor:
    FILTERS = {
        "blur": lambda img: img.filter(ImageFilter.GaussianBlur(radius=4)),
        "sharpen": lambda img: img.filter(ImageFilter.SHARPEN),
        "contour": lambda img: img.filter(ImageFilter.CONTOUR),
        "emboss": lambda img: img.filter(ImageFilter.EMBOSS),
        "edge_enhance": lambda img: img.filter(ImageFilter.EDGE_ENHANCE_MORE),
        "smooth": lambda img: img.filter(ImageFilter.SMOOTH_MORE),
        "detail": lambda img: img.filter(ImageFilter.DETAIL),
        "grayscale": lambda img: ImageOps.grayscale(img).convert("RGB"),
        "invert": lambda img: ImageOps.invert(img.convert("RGB")),
        "sepia": lambda img: PhotoEditor._sepia(img),
        "posterize": lambda img: ImageOps.posterize(img.convert("RGB"), 3),
        "solarize": lambda img: ImageOps.solarize(img.convert("RGB"), threshold=128),
        "vignette": lambda img: PhotoEditor._vignette(img),
    }

    def __init__(self, source):
        """source can be a filepath, bytes, or a file-like object."""
        if isinstance(source, (bytes, bytearray)):
            self.image = Image.open(BytesIO(source)).convert("RGB")
        else:
            self.image = Image.open(source).convert("RGB")

    # ---------- Filters ----------

    def apply_filter(self, name: str):
        name = name.lower()
        if name not in self.FILTERS:
            raise ValueError(
                f"Unknown filter '{name}'. Available: {', '.join(self.FILTERS)}"
            )
        self.image = self.FILTERS[name](self.image)
        return self

    @staticmethod
    def _sepia(img: Image.Image) -> Image.Image:
        gray = ImageOps.grayscale(img)
        sepia = ImageOps.colorize(gray, black="#3a2410", white="#ffe8c2")
        return sepia.convert("RGB")

    @staticmethod
    def _vignette(img: Image.Image, strength: float = 0.85) -> Image.Image:
        w, h = img.size
        mask = Image.new("L", (w, h), 0)
        px = mask.load()
        cx, cy = w / 2, h / 2
        max_dist = ((cx ** 2) + (cy ** 2)) ** 0.5
        for y in range(h):
            for x in range(w):
                dist = (((x - cx) ** 2) + ((y - cy) ** 2)) ** 0.5
                factor = 1 - (dist / max_dist) * strength
                px[x, y] = max(0, min(255, int(255 * factor)))
        black = Image.new("RGB", (w, h), (0, 0, 0))
        return Image.composite(img, black, mask)

    # ---------- Color temperature ----------

    def adjust_temperature(self, amount: int):
        """
        amount: -100 (cool/blue) to +100 (warm/orange). 0 = no change.
        """
        amount = max(-100, min(100, amount))
        r, g, b = self.image.split()
        r_shift = int(amount * 0.6)
        b_shift = int(-amount * 0.6)

        r = r.point(lambda i: max(0, min(255, i + r_shift)))
        b = b.point(lambda i: max(0, min(255, i + b_shift)))

        self.image = Image.merge("RGB", (r, g, b))
        return self

    # ---------- Adjustments ----------

    def adjust_brightness(self, factor: float):
        """factor: 1.0 = unchanged, <1 darker, >1 brighter."""
        self.image = ImageEnhance.Brightness(self.image).enhance(factor)
        return self

    def adjust_contrast(self, factor: float):
        self.image = ImageEnhance.Contrast(self.image).enhance(factor)
        return self

    def adjust_saturation(self, factor: float):
        """factor: 0 = grayscale, 1 = unchanged, >1 more saturated."""
        self.image = ImageEnhance.Color(self.image).enhance(factor)
        return self

    def adjust_sharpness(self, factor: float):
        self.image = ImageEnhance.Sharpness(self.image).enhance(factor)
        return self

    # ---------- Resolution / geometry ----------

    def resize(self, width: int, height: int = None, keep_aspect: bool = True):
        if height is None or keep_aspect:
            self.image.thumbnail(
                (width, height or width), Image.LANCZOS
            )
        else:
            self.image = self.image.resize((width, height), Image.LANCZOS)
        return self

    def crop_square(self):
        w, h = self.image.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        self.image = self.image.crop((left, top, left + side, top + side))
        return self

    def rotate(self, degrees: float, expand: bool = True):
        self.image = self.image.rotate(-degrees, expand=expand)
        return self

    def flip_horizontal(self):
        self.image = ImageOps.mirror(self.image)
        return self

    def flip_vertical(self):
        self.image = ImageOps.flip(self.image)
        return self

    # ---------- Output ----------

    def to_bytes(self, fmt: str = "PNG") -> bytes:
        buf = BytesIO()
        self.image.save(buf, format=fmt)
        buf.seek(0)
        return buf.read()

    def to_bytes_under_limit(self, max_bytes: int, fmt: str = "PNG") -> tuple[bytes, str]:
        """
        Export the image, guaranteed to be <= max_bytes.

        Strategy:
          1. Try PNG as-is.
          2. If too big, switch to JPEG and step quality down (95 -> 40).
          3. If still too big at lowest quality, progressively downscale
             resolution (90% steps) and retry JPEG compression.

        Returns (bytes, extension) — extension is "png" or "jpg" depending
        on which path succeeded, since transparency is lost once we fall
        back to JPEG.
        """
        # 1. Try original format first (cheap, preserves quality/alpha)
        data = self.to_bytes(fmt)
        if len(data) <= max_bytes:
            return data, fmt.lower()

        # 2. Fall back to JPEG, stepping quality down
        working = self.image.convert("RGB")
        for quality in (95, 85, 75, 65, 55, 45, 40):
            buf = BytesIO()
            working.save(buf, format="JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) <= max_bytes:
                return data, "jpg"

        # 3. Still too big — progressively downscale resolution
        scale = 0.9
        while scale > 0.1:
            w, h = working.size
            resized = working.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))),
                Image.LANCZOS,
            )
            buf = BytesIO()
            resized.save(buf, format="JPEG", quality=60, optimize=True)
            data = buf.getvalue()
            if len(data) <= max_bytes:
                return data, "jpg"
            working = resized
            scale *= 0.9

        # Last resort: return smallest attempt even if still over (caller can reject)
        return data, "jpg"

    def save(self, path: str):
        self.image.save(path)
        return self


def _build_arg_parser():
    parser = argparse.ArgumentParser(description="Edit an image with Pillow.")
    parser.add_argument("input", help="Path to input image")
    parser.add_argument("output", help="Path to save edited image")
    parser.add_argument("--filter", choices=list(PhotoEditor.FILTERS.keys()))
    parser.add_argument("--temp", type=int, default=0, help="-100 to 100")
    parser.add_argument("--brightness", type=float, default=1.0)
    parser.add_argument("--contrast", type=float, default=1.0)
    parser.add_argument("--saturation", type=float, default=1.0)
    parser.add_argument("--sharpness", type=float, default=1.0)
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--rotate", type=float, default=0)
    parser.add_argument("--flip-h", action="store_true")
    parser.add_argument("--flip-v", action="store_true")
    parser.add_argument("--square", action="store_true")
    return parser


def main():
    args = _build_arg_parser().parse_args()
    editor = PhotoEditor(args.input)

    if args.filter:
        editor.apply_filter(args.filter)
    if args.temp:
        editor.adjust_temperature(args.temp)
    if args.brightness != 1.0:
        editor.adjust_brightness(args.brightness)
    if args.contrast != 1.0:
        editor.adjust_contrast(args.contrast)
    if args.saturation != 1.0:
        editor.adjust_saturation(args.saturation)
    if args.sharpness != 1.0:
        editor.adjust_sharpness(args.sharpness)
    if args.square:
        editor.crop_square()
    if args.width:
        editor.resize(args.width, args.height)
    if args.rotate:
        editor.rotate(args.rotate)
    if args.flip_h:
        editor.flip_horizontal()
    if args.flip_v:
        editor.flip_vertical()

    editor.save(args.output)
    print(f"Saved edited image to {args.output}")


if __name__ == "__main__":
    main()
