"""Run the complete chair display sequence without MQTT or a payment website."""

import argparse
import sys
import time
from pathlib import Path

from yoctopuce import yocto_api, yocto_display

from chair_controller import DisplayView


SCRIPT_DIR = Path(__file__).resolve().parent
IDLE_SECONDS = 30
PROCESSING_SECONDS = 30
SETUP_SECONDS = 60


def parse_args():
    parser = argparse.ArgumentParser(
        description="Preview the complete idle-to-idle display sequence."
    )
    parser.add_argument(
        "--hub",
        default="127.0.0.1",
        help='VirtualHub address; use "usb" for direct USB',
    )
    parser.add_argument(
        "--qr",
        type=Path,
        default=SCRIPT_DIR / "qrcode.bmp",
        help="64x64 QR image (default: qrcode.bmp beside this script)",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        choices=(10, 20),
        default=10,
        help="Paid session duration to preview (default: 10)",
    )
    return parser.parse_args()


def wait_while_online(display, seconds, errmsg, update=None, fast=False):
    """Wait for a phase while optionally redrawing its countdown/animation."""
    started = time.monotonic()
    last_remaining = None
    while display.isOnline():
        elapsed = time.monotonic() - started
        remaining = max(0, int(seconds - elapsed + 0.999))
        if update is not None and (fast or remaining != last_remaining):
            update(remaining)
            last_remaining = remaining
        if elapsed >= seconds:
            return True
        yocto_api.YAPI.Sleep(1 if fast else 20, errmsg)
    return False


def main():
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

        view = DisplayView(display, qr_path)

        print("Idle welcome screen: 30 seconds")
        view.idle()
        if not wait_while_online(display, IDLE_SECONDS, errmsg):
            return 1

        print("Idle-to-processing transition; processing: 30 seconds")
        view.processing()
        if not wait_while_online(
            display, PROCESSING_SECONDS, errmsg, lambda _remaining: view.processing()
        ):
            return 1

        print("Processing-to-setup transition; setup countdown: 60 seconds")
        view.transition_to_setup()
        if not wait_while_online(
            display,
            SETUP_SECONDS,
            errmsg,
            lambda remaining: view.countdown("SET UP THE CHAIR", remaining),
        ):
            return 1
        view.countdown("SET UP THE CHAIR", 0)

        massage_seconds = args.minutes * 60
        print(f"Setup-to-massage transition; massage countdown: {args.minutes} minutes")
        view.transition_to_massage(args.minutes)
        if not wait_while_online(
            display, massage_seconds, errmsg, view.massage_countdown, fast=True
        ):
            return 1
        view.massage_countdown(0)

        print("Massage-to-idle transition; final idle screen: 30 seconds")
        view.transition_to_idle()
        if not wait_while_online(display, IDLE_SECONDS, errmsg):
            return 1

        print("Complete display sequence finished.")
        return 0
    except KeyboardInterrupt:
        print("Display test stopped")
        return 0
    finally:
        yocto_api.YAPI.FreeAPI()


if __name__ == "__main__":
    raise SystemExit(main())
