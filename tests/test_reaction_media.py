"""Real encodes verify the saved image fits without losing orientation or alpha."""

from io import BytesIO
import random

import pytest
from PIL import Image

import reaction_media
from reaction_media import ImageCompressionError, prepare_upload


def encode(image, fmt="PNG", **options):
    output = BytesIO()
    image.save(output, format=fmt, **options)
    return output.getvalue()


def test_files_that_fit_remain_byte_for_byte_unchanged():
    data = b"GIF89a existing reaction"
    assert prepare_upload(data, ".gif", len(data)) == (data, ".gif")


def test_opaque_png_becomes_jpeg_without_resizing_when_quality_is_enough():
    data = encode(Image.new("RGB", (256, 128), "orange"), compress_level=0)
    result, ext = prepare_upload(data, ".png", 4096)
    assert len(result) <= 4096 < len(data)
    assert ext == ".jpg"
    with Image.open(BytesIO(result)) as saved:
        assert saved.format == "JPEG" and saved.size == (256, 128)


@pytest.mark.parametrize("mode", ["RGBA", "P"])
def test_transparency_is_preserved(mode):
    image = Image.new("RGBA", (128, 128), (255, 0, 0, 64))
    if mode == "P":
        image = image.convert("P")
    data = encode(image, compress_level=0)
    result, ext = prepare_upload(data, ".png", 1024)
    assert ext == ".webp" and len(result) <= 1024 < len(data)
    with Image.open(BytesIO(result)) as saved:
        assert saved.convert("RGBA").getchannel("A").getextrema() == (64, 64)


def test_noisy_image_is_resized_when_quality_changes_are_insufficient():
    image = Image.frombytes("RGB", (256, 256), random.Random(0).randbytes(256 * 256 * 3))
    result, _ = prepare_upload(encode(image), ".png", 4096)
    assert len(result) <= 4096
    with Image.open(BytesIO(result)) as saved:
        assert saved.width < 256 and saved.height == saved.width


def test_exif_orientation_is_applied_before_metadata_is_removed():
    image = Image.new("RGB", (80, 40), "blue")
    exif = Image.Exif()
    exif[274] = 6
    data = encode(image, "JPEG", exif=exif) + b"padding" * 1000
    result, _ = prepare_upload(data, ".jpg", 2048)
    with Image.open(BytesIO(result)) as saved:
        assert saved.size == (40, 80)
        assert saved.getexif().get(274) is None


def test_oversized_animation_is_refused_without_flattening():
    image = Image.new("RGB", (16, 16), "red")
    data = encode(image, "GIF", save_all=True, append_images=[Image.new("RGB", (16, 16), "blue")], duration=100)
    with pytest.raises(ImageCompressionError, match="animation"):
        prepare_upload(data, ".gif", 10)


def test_invalid_oversized_media_has_a_useful_error():
    with pytest.raises(ImageCompressionError, match="readable still images"):
        prepare_upload(b"not an image", ".mp4", 5)


def test_pixel_and_source_limits_are_enforced(monkeypatch):
    monkeypatch.setattr(reaction_media, "MAX_IMAGE_PIXELS", 100)
    data = encode(Image.new("RGB", (16, 16)))
    with pytest.raises(ImageCompressionError, match="megapixels"):
        prepare_upload(data, ".png", 10)
    monkeypatch.setattr(reaction_media, "SOURCE_MAX_BYTES", 16)
    with pytest.raises(ImageCompressionError, match="Originals for compression"):
        prepare_upload(b"x" * 17, ".png", 10)
    assert prepare_upload(b"x" * 20, ".png", 20) == (b"x" * 20, ".png")


def test_impossibly_small_limit_fails_after_bounded_attempts():
    data = encode(Image.new("RGB", (8, 8), "red"))
    with pytest.raises(ImageCompressionError, match="could not be compressed enough"):
        prepare_upload(data, ".png", 10)
