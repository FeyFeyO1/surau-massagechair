"""Optional MQTT status display for the Yocto-MaxiDisplay chair timer."""

from pathlib import Path
from queue import Empty, SimpleQueue
import argparse
import json
import os
import sys
import time

import paho.mqtt.client as mqtt
from yoctopuce import yocto_api, yocto_display

from welcome_display import QR_SIZE, massage_sprite_as_gif, welcome_as_gif


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_BROKER = "minecraft.duniapalsu.com"
DEFAULT_TOPIC = "surau/setia-eco-glades/massage-chair/1/session"
VALID_SESSION_MINUTES = {10, 20}
SETUP_SECONDS = 60
MASSAGE_ANIMATION_FPS = 30
PROCESSING_DOTS = (".", "..", "...")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--broker", default=os.getenv("MQTT_HOST", DEFAULT_BROKER))
    parser.add_argument("--port", type=int, default=int(os.getenv("MQTT_PORT", "1883")))
    parser.add_argument("--topic", default=os.getenv("MQTT_TOPIC", DEFAULT_TOPIC))
    parser.add_argument("--username", default=os.getenv("MQTT_USERNAME"))
    parser.add_argument("--password", default=os.getenv("MQTT_PASSWORD"))
    parser.add_argument("--hub", default="usb")
    parser.add_argument("--qr", type=Path, default=SCRIPT_DIR / "qrcode.bmp")
    return parser.parse_args()


class DisplayView:
    def __init__(self, display, qr_path):
        self.display = display
        self.layer = display.get_displayLayer(0)
        self.width = display.get_displayWidth()
        self.height = display.get_displayHeight()
        self.qr_path = qr_path
        self.last_view = None
        self.processing_frame = 0
        self.processing_next_frame = 0.0
        self.processing_buffer = None
        self.countdown_visible = None
        self.countdown_buffer = None
        self.massage_buffer = None
        self.massage_visible_index = 1
        self.massage_buffer_index = 2
        self.massage_frame = 0
        self.massage_next_frame = 0.0
        self.massage_frame_names = None

    def reset(self):
        self.display.resetAll()
        self.layer = self.display.get_displayLayer(0)
        self.layer.unhide()
        self.layer.clear()
        self.layer.selectGrayPen(255)

    def idle(self):
        if self.last_view == "idle":
            return
        self.reset()
        self._draw_idle_content(self.layer)
        self.last_view = "idle"

    def _draw_idle_content(self, layer):
        """Draw the production QR welcome screen onto any layer."""
        name = "chair-idle.gif"
        self.display.upload(name, welcome_as_gif(self.qr_path, self.width, self.height))
        layer.drawImage(0, 0, name)
        center = QR_SIZE + (self.width - QR_SIZE) // 2
        layer.selectGrayPen(255)
        layer.selectFont("8x8.yfm")
        layer.drawText(center, 6, yocto_display.YDisplayLayer.ALIGN.CENTER, "MASSAGE")
        layer.drawText(center, 17, yocto_display.YDisplayLayer.ALIGN.CENTER, "CHAIR")
        layer.selectFont("Small.yfm")
        layer.drawText(center, 58, yocto_display.YDisplayLayer.ALIGN.CENTER, "Scan QR to pay")

    def _draw_countdown_content(self, layer, heading, remaining_seconds, timer_x=None):
        """Draw a boxed heading and large countdown without resetting layers."""
        minutes, seconds = divmod(max(0, remaining_seconds), 60)
        layer.selectGrayPen(255)
        layer.drawRect(1, 1, self.width - 2, 15)
        layer.selectFont("Small.yfm")
        layer.drawText(
            self.width // 2,
            8,
            yocto_display.YDisplayLayer.ALIGN.CENTER,
            heading,
        )
        layer.selectFont("Large.yfm")
        layer.drawText(
            timer_x if timer_x is not None else self.width // 2,
            40,
            yocto_display.YDisplayLayer.ALIGN.CENTER,
            f"{minutes:02}:{seconds:02}",
        )

    def _slide_from_bottom(self, incoming):
        """Reveal an already-drawn layer with a bottom-to-top scroll."""
        incoming.setLayerPosition(0, self.height, 0)
        incoming.unhide()
        transition_error = yocto_api.YRefParam()
        for y in range(self.height, -1, -2):
            incoming.setLayerPosition(0, y, 0)
            yocto_api.YAPI.Sleep(15, transition_error)
        incoming.setLayerPosition(0, 0, 0)

    def _slide_up_and_off(self, outgoing):
        """Move the current foreground upward to reveal layer 0 beneath it."""
        outgoing.setLayerPosition(0, 0, 0)
        outgoing.unhide()
        transition_error = yocto_api.YRefParam()
        for y in range(0, -self.height - 1, -2):
            outgoing.setLayerPosition(0, y, 0)
            yocto_api.YAPI.Sleep(15, transition_error)
        outgoing.hide()

    def _push_up(self, outgoing, incoming):
        """Move outgoing up while incoming enters from below in lockstep."""
        outgoing.setLayerPosition(0, 0, 0)
        outgoing.unhide()
        incoming.setLayerPosition(0, self.height, 0)
        incoming.unhide()
        transition_error = yocto_api.YRefParam()
        for distance in range(0, self.height + 1, 2):
            outgoing.setLayerPosition(0, -distance, 0)
            incoming.setLayerPosition(0, self.height - distance, 0)
            yocto_api.YAPI.Sleep(15, transition_error)
        outgoing.hide()
        incoming.setLayerPosition(0, 0, 0)

    def _box_wipe_to_center(self, mask, buffer_layer):
        """Close a double-buffered black mask from the edges to the center."""
        mask.clear()
        mask.setLayerPosition(0, 0, 0)
        mask.unhide()
        buffer_layer.clear()
        buffer_layer.setLayerPosition(0, 0, 0)
        buffer_layer.hide()
        transition_error = yocto_api.YRefParam()
        center_x = self.width // 2
        center_y = self.height // 2
        steps = 16
        for step in range(steps + 1):
            half_width = round((self.width / 2) * (1 - step / steps))
            half_height = round((self.height / 2) * (1 - step / steps))
            left = center_x - half_width
            right = center_x + half_width - 1
            top = center_y - half_height
            bottom = center_y + half_height - 1
            buffer_layer.clear()
            buffer_layer.selectGrayPen(0)
            if top > 0:
                buffer_layer.drawBar(0, 0, self.width - 1, top - 1)
            if bottom < self.height - 1:
                buffer_layer.drawBar(0, bottom + 1, self.width - 1, self.height - 1)
            if left > 0 and top <= bottom:
                buffer_layer.drawBar(0, top, left - 1, bottom)
            if right < self.width - 1 and top <= bottom:
                buffer_layer.drawBar(right + 1, top, self.width - 1, bottom)
            self.display.swapLayerContent(2, 3)
            yocto_api.YAPI.Sleep(10, transition_error)

        buffer_layer.clear()
        buffer_layer.selectGrayPen(0)
        buffer_layer.drawBar(0, 0, self.width - 1, self.height - 1)
        self.display.swapLayerContent(2, 3)

    def message(self, heading, detail=""):
        key = (heading, detail)
        if self.last_view == key:
            return
        self.reset()
        self.layer.selectFont("Medium.yfm")
        self.layer.drawText(self.width // 2, 20, yocto_display.YDisplayLayer.ALIGN.CENTER, heading)
        if detail:
            self.layer.selectFont("Small.yfm")
            self.layer.drawText(self.width // 2, 43, yocto_display.YDisplayLayer.ALIGN.CENTER, detail)
        self.last_view = key

    def processing(self):
        """Show the full-screen animated Processing Payment page."""
        now = time.monotonic()
        if self.last_view != "processing":
            if self.last_view == "idle":
                self.transition_to_processing()
            self.display.resetAll()
            base = self.display.get_displayLayer(0)
            base.clear()
            visible = self.display.get_displayLayer(1)
            visible.clear()
            visible.unhide()
            self.processing_buffer = self.display.get_displayLayer(2)
            self.processing_buffer.clear()
            self.processing_buffer.hide()
            self.processing_frame = 0
            self.processing_next_frame = 0.0
            self.last_view = "processing"

        if now < self.processing_next_frame:
            return

        layer = self.processing_buffer
        layer.clear()
        layer.selectGrayPen(255)
        layer.selectFont("Medium.yfm")
        layer.drawText(self.width // 2, 14,
                       yocto_display.YDisplayLayer.ALIGN.CENTER, "Processing")
        layer.drawText(self.width // 2, 32,
                       yocto_display.YDisplayLayer.ALIGN.CENTER, "Payment")
        layer.drawText(
            self.width // 2,
            50,
            yocto_display.YDisplayLayer.ALIGN.CENTER,
            PROCESSING_DOTS[self.processing_frame],
        )
        self.display.swapLayerContent(1, 2)
        self.processing_frame = (self.processing_frame + 1) % len(PROCESSING_DOTS)
        self.processing_next_frame = now + 0.45

    def transition_to_processing(self):
        """Cover the idle screen with a box expanding from the center."""
        wipe = self.display.get_displayLayer(1)
        wipe.clear()
        wipe.unhide()
        wipe.selectGrayPen(0)
        transition_error = yocto_api.YRefParam()
        steps = 16
        center_x = self.width // 2
        center_y = self.height // 2
        for step in range(1, steps + 1):
            half_width = max(1, round((self.width / 2) * step / steps))
            half_height = max(1, round((self.height / 2) * step / steps))
            wipe.clear()
            wipe.selectGrayPen(0)
            wipe.drawBar(
                center_x - half_width,
                center_y - half_height,
                center_x + half_width - 1,
                center_y + half_height - 1,
            )
            yocto_api.YAPI.Sleep(20, transition_error)
        self.last_view = "processing-transition"

    def countdown(self, heading, remaining_seconds):
        """Update a boxed countdown using the setup screen's back buffer."""
        minutes, seconds = divmod(max(0, remaining_seconds), 60)
        detail = f"{minutes:02}:{seconds:02}"
        key = ("countdown", heading, detail)
        if self.last_view == key:
            return

        if self.countdown_visible is None or self.countdown_buffer is None:
            self.reset()
            self.countdown_visible = self.display.get_displayLayer(1)
            self.countdown_visible.clear()
            self.countdown_visible.unhide()
            self.countdown_buffer = self.display.get_displayLayer(2)
            self.countdown_buffer.clear()
            self.countdown_buffer.hide()

        self.countdown_buffer.clear()
        self._draw_countdown_content(
            self.countdown_buffer, heading, remaining_seconds
        )
        self.display.swapLayerContent(1, 2)
        self.last_view = key

    def transition_to_setup(self):
        """Push processing up while the 01:00 setup screen enters below."""
        setup = self.display.get_displayLayer(2)
        setup.clear()
        setup.hide()
        self._draw_countdown_content(setup, "SET UP THE CHAIR", SETUP_SECONDS)
        processing = self.display.get_displayLayer(1)
        self._push_up(processing, setup)
        # Keep the two layers as a visible/back-buffer pair so subsequent
        # countdown seconds do not need resetAll(), which makes the OLED blink.
        self.countdown_visible = setup
        self.countdown_buffer = processing
        self.last_view = (
            "countdown", "SET UP THE CHAIR", f"{SETUP_SECONDS // 60:02}:00"
        )

    def massage_countdown(self, remaining_seconds):
        """Draw the large timer beside a rapidly shaking chair sprite."""
        minutes, seconds = divmod(max(0, remaining_seconds), 60)
        detail = f"{minutes:02}:{seconds:02}"
        now = time.monotonic()

        if self.last_view != "massage-countdown":
            if self.massage_frame_names is None:
                self.massage_frame_names = []
                for index, (offset_x, offset_y) in enumerate(
                    ((0, 0), (1, -1), (1, 0), (0, 0))
                ):
                    name = f"massage-shake-{index}.gif"
                    self.display.upload(
                        name,
                        massage_sprite_as_gif(
                            self.width, self.height, offset_x, offset_y
                        ),
                    )
                    self.massage_frame_names.append(name)
            if self.last_view == "massage-transition":
                # Continue directly from the transition's final frame, which
                # has already been handed back to the normal layer 1/2 pair.
                self.massage_visible_index = 1
                self.massage_buffer_index = 2
                self.massage_buffer = self.display.get_displayLayer(2)
                self.massage_buffer.setLayerPosition(0, 0, 0)
                self.massage_buffer.clear()
                self.massage_buffer.hide()
            else:
                self.display.resetAll()
                base = self.display.get_displayLayer(0)
                base.clear()
                visible = self.display.get_displayLayer(1)
                visible.clear()
                visible.unhide()
                self.massage_visible_index = 1
                self.massage_buffer_index = 2
                self.massage_buffer = self.display.get_displayLayer(2)
                self.massage_buffer.clear()
                self.massage_buffer.hide()
            self.massage_frame = 0
            self.massage_next_frame = 0.0
            self.last_view = "massage-countdown"

        # Sending full-screen GIF and text commands every 1 ms can overrun the
        # MaxiDisplay command queue and expose a cleared buffer. A steady 30
        # FPS remains smooth while allowing each complete frame to be drawn.
        if now < self.massage_next_frame:
            return

        layer = self.massage_buffer
        layer.clear()
        layer.drawImage(0, 0, self.massage_frame_names[self.massage_frame])
        layer.selectGrayPen(255)
        layer.drawRect(1, 1, self.width - 2, 15)
        layer.selectFont("Small.yfm")
        layer.drawText(
            self.width // 2,
            8,
            yocto_display.YDisplayLayer.ALIGN.CENTER,
            "MASSAGE TIMER",
        )
        layer.selectFont("Large.yfm")
        layer.drawText(
            40,
            40,
            yocto_display.YDisplayLayer.ALIGN.CENTER,
            detail,
        )
        self.display.swapLayerContent(
            self.massage_visible_index, self.massage_buffer_index
        )
        self.massage_frame = (self.massage_frame + 1) % len(self.massage_frame_names)
        self.massage_next_frame = now + (1 / MASSAGE_ANIMATION_FPS)

    def transition_to_massage(self, minutes):
        """Morph setup into massage, then roll 00:00 into paid time."""
        # Setup is already visible on layer 2. Build the first massage frame
        # invisibly on layer 3, then swap complete frames without a black gap.
        visible = self.display.get_displayLayer(2)
        visible.unhide()
        buffer_layer = self.display.get_displayLayer(3)
        buffer_layer.clear()
        buffer_layer.hide()
        transition_error = yocto_api.YRefParam()

        def draw_frame(sprite_name, old_heading_x, new_heading_x,
                       timer_x, timer_y, timer_text):
            buffer_layer.clear()
            buffer_layer.drawImage(0, 0, sprite_name)
            buffer_layer.selectGrayPen(255)
            buffer_layer.drawRect(1, 1, self.width - 2, 15)
            buffer_layer.selectFont("Small.yfm")
            if old_heading_x is not None:
                buffer_layer.drawText(
                    old_heading_x, 8,
                    yocto_display.YDisplayLayer.ALIGN.CENTER,
                    "SET UP THE CHAIR",
                )
            buffer_layer.drawText(
                new_heading_x, 8,
                yocto_display.YDisplayLayer.ALIGN.CENTER,
                "MASSAGE TIMER",
            )
            if timer_text:
                buffer_layer.selectFont("Large.yfm")
                buffer_layer.drawText(
                    timer_x, timer_y,
                    yocto_display.YDisplayLayer.ALIGN.CENTER,
                    timer_text,
                )
            self.display.swapLayerContent(2, 3)
            # Yield for only 1 ms; display/USB throughput sets the real FPS.
            yocto_api.YAPI.Sleep(1, transition_error)

        entrance_names = []
        steps = 12
        for step in range(steps + 1):
            progress = step / steps
            sprite_offset = round(42 * (1 - progress))
            name = f"massage-enter-{step}.gif"
            self.display.upload(
                name,
                massage_sprite_as_gif(
                    self.width, self.height, sprite_offset, 0
                ),
            )
            entrance_names.append(name)

        # Headings cross horizontally, 00:00 moves left, chair enters right.
        for step, name in enumerate(entrance_names):
            progress = step / steps
            draw_frame(
                name,
                round(64 - 96 * progress),
                round(160 - 96 * progress),
                round(64 - 24 * progress),
                40,
                "00:00",
            )

        final_sprite = entrance_names[-1]
        # Roll the expired setup time upward until it disappears.
        for timer_y in range(38, 27, -2):
            draw_frame(final_sprite, None, 64, 40, timer_y, "00:00")
        draw_frame(final_sprite, None, 64, 40, 40, None)
        # Roll the paid duration upward from below into its final position.
        for timer_y in range(58, 39, -2):
            draw_frame(
                final_sprite, None, 64, 40, timer_y, f"{minutes:02}:00"
            )

        # Hand the completed screen back to the standard foreground layer.
        # Layer 1 is revealed before layer 2 is hidden, preventing a blank gap.
        handoff = self.display.get_displayLayer(1)
        # Layer 1 previously scrolled processing off the top and is still at
        # y=-height unless explicitly returned to the display origin.
        handoff.setLayerPosition(0, 0, 0)
        handoff.clear()
        handoff.drawImage(0, 0, final_sprite)
        handoff.selectGrayPen(255)
        handoff.drawRect(1, 1, self.width - 2, 15)
        handoff.selectFont("Small.yfm")
        handoff.drawText(
            64, 8, yocto_display.YDisplayLayer.ALIGN.CENTER, "MASSAGE TIMER"
        )
        handoff.selectFont("Large.yfm")
        handoff.drawText(
            40, 40, yocto_display.YDisplayLayer.ALIGN.CENTER,
            f"{minutes:02}:00",
        )
        handoff.unhide()
        visible.hide()
        buffer_layer.clear()
        buffer_layer.hide()

        self.last_view = "massage-transition"

    def transition_to_idle(self):
        """Close massage into a center box, then raise idle from below."""
        massage = self.display.get_displayLayer(1)
        massage.setLayerPosition(0, 0, 0)
        massage.unhide()
        idle = self.display.get_displayLayer(2)
        mask_buffer = self.display.get_displayLayer(3)
        self._box_wipe_to_center(idle, mask_buffer)

        # The opaque mask now covers the old screen, so it can be discarded.
        massage.clear()
        mask_buffer.setLayerPosition(0, self.height, 0)
        self._draw_idle_content(mask_buffer)
        mask_buffer.unhide()
        transition_error = yocto_api.YRefParam()
        for y in range(self.height, -1, -2):
            mask_buffer.setLayerPosition(0, y, 0)
            yocto_api.YAPI.Sleep(15, transition_error)
        mask_buffer.setLayerPosition(0, 0, 0)
        idle.clear()
        idle.hide()
        self.last_view = "idle-transition"


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
    display = yocto_display.YDisplay.FirstDisplay()
    if display is None or not display.isOnline():
        print("No online display connected", file=sys.stderr)
        yocto_api.YAPI.FreeAPI()
        return 1

    events = SimpleQueue()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="surau-chair-1")
    if args.username:
        client.username_pw_set(args.username, args.password)

    def on_connect(connected_client, _userdata, _flags, reason_code, _properties):
        if reason_code == 0:
            print(f"MQTT connected; subscribed to {args.topic}")
            connected_client.subscribe(args.topic, qos=1)
        else:
            print(f"MQTT connection rejected: {reason_code}", file=sys.stderr)

    def on_message(_client, _userdata, message):
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            state = payload.get("state")
            minutes = int(payload.get("minutes") or 0)
            valid_state = state in {"idle", "processing", "setup", "active"}
            valid_duration = state == "idle" or minutes in VALID_SESSION_MINUTES
            if payload.get("chairId") == "chair-1" and valid_state and valid_duration:
                events.put(payload)
            elif payload.get("chairId") == "chair-1":
                print(f"Ignored MQTT session with invalid duration: {minutes}", file=sys.stderr)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            print(f"Ignored invalid MQTT message: {error}", file=sys.stderr)

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.broker, args.port, keepalive=60)
    client.loop_start()

    view = DisplayView(display, qr_path)
    current = {"state": "idle", "minutes": 0, "phaseEndsAt": None}
    previous_state = "idle"
    view.idle()

    try:
        while display.isOnline():
            try:
                while True:
                    incoming = events.get_nowait()
                    current = incoming
            except Empty:
                pass

            state = current.get("state", "idle")
            minutes = int(current.get("minutes") or 0)
            now_ms = int(time.time() * 1000)

            if state == "idle":
                if previous_state == "active":
                    view.transition_to_idle()
                else:
                    view.idle()
            elif state == "processing":
                view.processing()
            elif state == "setup":
                remaining = max(0, (int(current["phaseEndsAt"]) - now_ms + 999) // 1000)
                if previous_state == "processing":
                    view.transition_to_setup()
                else:
                    view.countdown("SET UP THE CHAIR", remaining)
            elif state == "active":
                remaining = max(0, (int(current["phaseEndsAt"]) - now_ms + 999) // 1000)
                if previous_state == "setup":
                    view.transition_to_massage(minutes)
                else:
                    view.massage_countdown(remaining)

            previous_state = state

            # Run active animation as fast as the display accepts frames. Other
            # states need only modest polling frequency.
            yocto_api.YAPI.Sleep(1 if state == "active" else 100, errmsg)
        return 1
    except KeyboardInterrupt:
        print("Controller stopped")
        return 0
    finally:
        client.loop_stop()
        client.disconnect()
        yocto_api.YAPI.FreeAPI()


if __name__ == "__main__":
    raise SystemExit(main())
