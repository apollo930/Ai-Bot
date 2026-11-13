"""Fit oversized still images into the upload allowance used by saved reactions."""

from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError


# Allow a larger original for compression without downloading unbounded Nitro uploads.
SOURCE_MAX_BYTES = 50 * 2**20
MAX_IMAGE_PIXELS = 32_000_000


class ImageCompressionError(ValueError):
    """An upload could not be made small enough without discarding its content."""


def prepare_upload(data: bytes, extension: str, max_bytes: int) -> tuple[bytes, str]:
    """Leave files that fit untouched; compress oversized still images, preserving alpha."""
    if len(data) > max(max_bytes, SOURCE_MAX_BYTES):
        raise ImageCompressionError(f"Originals for compression must be at most {SOURCE_MAX_BYTES / 2**20:g} MiB.")
    if len(data) <= max_bytes:
        return data, extension

    try:
        with Image.open(BytesIO(data)) as original:
            if getattr(original, "n_frames", 1) > 1:
                raise ImageCompressionError("Automatic compression supports still images; resize animated images separately to keep their animation.")
            if original.width * original.height > MAX_IMAGE_PIXELS:
                raise ImageCompressionError("That image is too large to process. Resize it below 32 megapixels first.")
            with ImageOps.exif_transpose(original) as oriented:
                alpha = "A" in oriented.getbands() or "transparency" in oriented.info
                image = oriented.convert("RGBA" if alpha else "RGB")

        try:
            # WebP preserves transparency; opaque clipboard PNGs can become compact JPEGs.
            fmt, ext = ("WEBP", ".webp") if alpha else ("JPEG", ".jpg")
            options = {"method": 4} if alpha else {"optimize": True, "progressive": True}
            image.thumbnail((16_000, 16_000), Image.Resampling.LANCZOS)
            for _ in range(8):
                for quality in (95, 85, 75):
                    output = BytesIO()
                    image.save(output, format=fmt, quality=quality, **options)
                    if output.tell() <= max_bytes:
                        return output.getvalue(), ext
                size = (max(1, image.width * 3 // 4), max(1, image.height * 3 // 4))
                if size == image.size:
                    break
                smaller = image.resize(size, Image.Resampling.LANCZOS)
                image.close()
                image = smaller
        finally:
            image.close()
    except ImageCompressionError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
        raise ImageCompressionError("Only readable still images can be compressed automatically; resize this file separately.") from error

    raise ImageCompressionError("The image could not be compressed enough. Resize it further and try again.")
