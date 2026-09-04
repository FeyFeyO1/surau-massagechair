"""Animate a spinning massage-chair sprite beside the fixed payment QR."""

from io import BytesIO
from pathlib import Path
import argparse
import math
import sys
import time

from PIL import Image, ImageOps
from yoctopuce import yocto_api, yocto_display


SCRIPT_DIR = Path(__file__).resolve().parent
QR_SIZE = 64
FRAME_COUNT = 24

# The earlier, preferred 35x28 chair sprite from welcome_test.py.
CHAIR_SPRITE = (
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


def to_gif(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="GIF")
    return output.getvalue()


def qr_background(path: Path, width: int, height: int) -> bytes:
    with Image.open(path) as source:
        qr = ImageOps.exif_transpose(source).convert("L")
        if qr.size != (QR_SIZE, QR_SIZE):
            raise ValueError(f"QR must be 64x64, got {qr.width}x{qr.height}")
        qr = qr.point(lambda value: 255 if value >= 128 else 0, mode="1")
        screen = Image.new("1", (width, height), 0)
        screen.paste(qr, (0, 0))
        return to_gif(screen)


def chair_image() -> Image.Image:
    image = Image.new("1", (len(CHAIR_SPRITE[0]), len(CHAIR_SPRITE)), 0)
    pixels = image.load()
    for y, row in enumerate(CHAIR_SPRITE):
        for x, value in enumerate(row):
            if value == "#":
                pixels[x, y] = 1
    return image


def spin_frames(panel_width: int, panel_height: int):
    """Create a turntable illusion using perspective squash and mirroring."""
    chair = chair_image()
    frames = []
    for index in range(FRAME_COUNT):
        angle = math.tau * index / FRAME_COUNT
        facing = math.cos(angle)
        frame_width = max(2, round(chair.width * abs(facing)))
        source = chair if facing >= 0 else chair.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        squeezed = source.resize((frame_width, chair.height), Image.Resampling.NEAREST)

        frame = Image.new("1", (panel_width, panel_height), 0)
        x = (panel_width - frame_width) // 2
        y = (panel_height - chair.height) // 2
        frame.paste(squeezed, (x, y))
        frames.append(to_gif(frame))
    return frames


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hub", default="127.0.0.1",
                        help='VirtualHub address; use "usb" for direct USB')
    parser.add_argument("--qr", type=Path, default=SCRIPT_DIR / "qrcode.bmp")
    parser.add_argument("--fps", type=float, default=12.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.fps <= 0:
        print("--fps must be positive", file=sys.stderr)
        return 2
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
        panel_width = width - QR_SIZE
        if panel_width < 35 or height < 64:
            print(f"Unexpected display size: {width}x{height}", file=sys.stderr)
            return 1

        display.resetAll()
        background = display.get_displayLayer(0)
        background.clear()
        display.upload("chair-spin-background.gif", qr_background(qr_path, width, height))
        background.drawImage(0, 0, "chair-spin-background.gif")

        frame_names = []
        for index, frame in enumerate(spin_frames(panel_width, height)):
            name = f"chair-spin-{index:02}.gif"
            result = display.upload(name, frame)
            if result is not None and result < 0:
                print(f"Frame upload failed: {display.get_errorMessage()}", file=sys.stderr)
                return 1
            frame_names.append(name)

        visible = display.get_displayLayer(1)
        visible.clear()
        visible.setLayerPosition(QR_SIZE, 0, 0)
        back_buffer = display.get_displayLayer(2)
        back_buffer.clear()
        back_buffer.setLayerPosition(QR_SIZE, 0, 0)
        back_buffer.hide()

        period = 1.0 / args.fps
        frame_index = 0
        print(f"Spinning chair at {args.fps:g} FPS; press Ctrl+C to stop")
        while display.isOnline():
            started = time.monotonic()
            back_buffer.clear()
            back_buffer.drawImage(0, 0, frame_names[frame_index])
            display.swapLayerContent(1, 2)
            frame_index = (frame_index + 1) % len(frame_names)

            remaining = period - (time.monotonic() - started)
            if remaining > 0:
                yocto_api.YAPI.Sleep(max(1, int(remaining * 1000)), errmsg)

        print("Display disconnected", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Animation stopped")
        return 0
    finally:
        yocto_api.YAPI.FreeAPI()


if __name__ == "__main__":
    raise SystemExit(main())
