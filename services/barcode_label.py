"""Code 128 slide labels. A USB scanner submits the decoded text to the identity check."""

from __future__ import annotations

from io import BytesIO

# Code 128 Set B patterns, values 0-106. Each entry is bar/space widths.
_PATTERNS = [
    "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312", "132212", "221213",
    "221312", "231212", "112232", "122132", "122231", "113222", "123122", "123221", "223211", "221132",
    "221231", "213212", "223112", "312131", "311222", "321122", "321221", "312212", "322112", "322211",
    "212123", "212321", "232121", "111323", "131123", "131321", "112313", "132113", "132311", "211313",
    "231113", "231311", "112133", "112331", "132131", "113123", "113321", "133121", "313121", "211331",
    "231131", "213113", "213311", "213131", "311123", "311321", "331121", "312113", "312311", "332111",
    "314111", "221411", "431111", "111224", "111422", "121124", "121421", "141122", "141221", "112214",
    "112412", "122114", "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111",
    "111242", "121142", "121241", "114212", "124112", "124211", "411212", "421112", "421211", "212141",
    "214121", "412121", "111143", "111341", "131141", "114113", "114311", "411113", "411311", "113141",
    "114131", "311141", "411131", "211412", "211214", "211232", "2331112",
]


def encode_code128(text: str) -> list[int]:
    if not text or any(ord(char) < 32 or ord(char) > 126 for char in text):
        raise ValueError("Code 128 labels accept printable ASCII only")
    values = [ord(char) - 32 for char in text]
    symbols = [104, *values]
    checksum = 104
    for index, value in enumerate(values, start=1):
        checksum += index * value
    symbols.append(checksum % 103)
    symbols.append(106)
    return symbols


def render_code128_png(text: str, bar_width: int = 2, height: int = 80) -> bytes:
    from PIL import Image

    symbols = encode_code128(text)
    modules = "".join(_PATTERNS[value] for value in symbols)
    quiet = 10 * bar_width
    width = quiet * 2 + sum(int(module) for module in modules) * bar_width
    image = Image.new("L", (width, height), 255)
    pixels = image.load()
    x = quiet
    black = True
    for module in modules:
        run = int(module) * bar_width
        if black:
            for column in range(x, x + run):
                for row in range(height):
                    pixels[column, row] = 0
        x += run
        black = not black
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def render_datamatrix_png(text: str) -> bytes:
    from pystrich.datamatrix import DataMatrixEncoder

    encoder = DataMatrixEncoder(text)
    return encoder.get_imagedata()
