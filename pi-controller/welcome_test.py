"""Test a compact welcome message and QR code on the 128x64 display."""

from io import BytesIO
from pathlib import Path
import argparse
import sys

from PIL import Image, ImageDraw, ImageOps
from yoctopuce import yocto_api, yocto_display


SCRIPT_DIR = Path(__file__).resolve().parent
QR_SIZE = 64


def draw_massage_chair(image: Image.Image, center_x: int, top: int) -> None:
    """Draw the supplied chair artwork as an embedded 35x28 pixel sprite."""
    sprite = (
        ".....................##########....",
        "....................#......##.###..",
        "..................##..###..#.##....",
        "...................#..####..#..###.",
        "..................##.#####.#..####.",
        ".................###.####....#####.",
        ".................###..###.#.####...",
        "..............##.#####....#.###..#.",
        "................###.######.###..##.",
        "..........#..######.#####..#####...",
        "............#.####..##........##...",
        ".............#####.#....########...",
        ".......##.......#.....########.....",
        ".......##.#######.#..#######..#....",
        ".....#############...######...#....",
        ".....##.........###..####....#.....",
        ".......###.##.#...#.........##.#...",
        ".....###...###...#........####.....",
        "....###...##....##.......####.#....",
        "....###...##...####...######..#....",
        "...####..###...###..#######.##.....",
        "....##############..######.###.....",
        ".....#....##...##..#####..###...#..",
        "...##...##.....##..###..##....#####",
        "..##...##...#####.##..#.....#######",
        "#########...####.#..##..########...",
        ".##########.####.##..########......",
        "....###########.#######............",
    )
    draw = ImageDraw.Draw(image)
    left = center_x - len(sprite[0]) // 2
    for y, row in enumerate(sprite):
        for x, pixel in enumerate(row):
            if pixel == "#":
                draw.point((left + x, top + y), fill=1)


def welcome_as_gif(path: Path, display_width: int, display_height: int) -> bytes:
    """Place the full-resolution QR in a display-sized GIF."""
    with Image.open(path) as source:
        qr = ImageOps.exif_transpose(source).convert("L")
        # Do not resize this QR. Its modules are already close to the minimum
        # useful size, and non-integer scaling makes it unscannable.
        if qr.size != (QR_SIZE, QR_SIZE):
            raise ValueError(f"QR must be {QR_SIZE}x{QR_SIZE}, got {qr.size[0]}x{qr.size[1]}")
        qr = qr.point(lambda pixel: 255 if pixel >= 128 else 0, mode="1")

        image = Image.new("1", (display_width, display_height), 0)
        image.paste(qr, (0, 0))
        text_center = QR_SIZE + (display_width - QR_SIZE) // 2
        draw_massage_chair(image, text_center, 24)
        output = BytesIO()
        image.save(output, format="GIF")
        return output.getvalue()


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--hub",
        default="127.0.0.1",
        help='VirtualHub address; use "usb" for direct USB (default: 127.0.0.1)',
    )
    parser.add_argument(
        "--qr",
        type=Path,
        default=SCRIPT_DIR / "qrcode.bmp",
        help="QR image (default: qrcode.bmp beside this script)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    qr_path = args.qr.expanduser().resolve()
    if not qr_path.is_file():
        print(f"QR image not found: {qr_path}", file=sys.stderr)
        return 2

    errmsg = yocto_api.YRefParam()
    if yocto_api.YAPI.RegisterHub(args.hub, errmsg) != yocto_api.YAPI.SUCCESS:
        print(f"Cannot contact Yoctopuce hub: {errmsg.value}", file=sys.stderr)
        return 1

    try:
        display = yocto_display.YDisplay.FirstDisplay()
        if display is None or not display.isOnline():
            print("No online display connected", file=sys.stderr)
            return 1

        width = display.get_displayWidth()
        height = display.get_displayHeight()
        if width < QR_SIZE or height < 64:
            print(f"Unexpected display size: {width}x{height}", file=sys.stderr)
            return 1

        display.resetAll()
        layer = display.get_displayLayer(0)
        layer.unhide()
        layer.clear()
        layer.selectGrayPen(255)

        remote_name = "welcome-screen.gif"
        result = display.upload(remote_name, welcome_as_gif(qr_path, width, height))
        if result is not None and result < 0:
            print(f"QR upload failed: {display.get_errorMessage()}", file=sys.stderr)
            return 1

        result = layer.drawImage(0, 0, remote_name)
        if result is not None and result < 0:
            print(f"QR drawing failed: {display.get_errorMessage()}", file=sys.stderr)
            return 1

        text_center = QR_SIZE + (width - QR_SIZE) // 2
        layer.selectFont("8x8.yfm")
        layer.drawText(
            text_center,
            6,
            yocto_display.YDisplayLayer.ALIGN.CENTER,
            "MASSAGE",
        )
        layer.drawText(
            text_center,
            17,
            yocto_display.YDisplayLayer.ALIGN.CENTER,
            "CHAIR",
        )

        layer.selectFont("Small.yfm")
        layer.drawText(
            text_center,
            58,
            yocto_display.YDisplayLayer.ALIGN.CENTER,
            "Scan QR to pay",
        )

        print(f"Welcome screen displayed on {width}x{height}")
        return 0
    finally:
        yocto_api.YAPI.FreeAPI()


if __name__ == "__main__":
    raise SystemExit(main())
