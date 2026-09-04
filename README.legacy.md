# Surau Setia Eco Glades Massage Chair

## Project progress

Last updated: **9 August 2026**

### Completed

- Mobile-first Surau Setia Eco Glades payment website using the olive `#808000` and sandstone brand palette.
- Two session options: **10 minutes for RM5** and **20 minutes for RM10**.
- Guided three-step flow: choose a session, pay using the physical QR code on the wall, and submit proof of payment.
- Required phone number containing **10–15 digits only**.
- Required PNG, JPG, or PDF receipt upload with a 10 MB limit.
- Receipt storage through the Zipline API.
- Payment logging to Google Sheets with the phone number, minutes, price, and Zipline receipt URL.
- Animated validation screen followed by a completion screen and an option to start another session.
- Hidden complimentary 10-minute flow accessed by clicking **Feel renewed.** and entering the configured password.
- Windows launcher that detects an already-running server and avoids duplicate port-3003 processes.
- Clear browser errors for unavailable servers, Zipline failures, and Google Sheets failures.
- MQTT coordination between the backend, website countdown, and Raspberry Pi display controller.
- Single-chair locking while a session is processing, setting up, or active.
- A one-minute setup countdown followed by the paid 10- or 20-minute massage countdown.
- MQTT and Raspberry Pi controller validation rejects legacy paid durations other than 10 or 20 minutes.
- Display-only testing supports both production durations through `--minutes 10` or `--minutes 20`.
- MQTT watchdog automatically clears invalid retained messages, processing reservations older than three minutes, and setup or active states that remain past their deadline.
- Website language switcher supports English, Bahasa Malaysia, and Arabic, remembers the selected language, and enables right-to-left layout for Arabic.
- Separate hardened Linux Docker image and Compose service for the payment website/backend; the Raspberry Pi controller remains in its own image.
- Raspberry Pi controller defaults to the installed Shelly Plus 1PM MQTT RPC topic and confirms command publication before recording the relay state.

### Current architecture

1. The browser submits the phone number, session duration, and receipt to the local Node.js server.
2. The server validates all submitted fields.
3. The receipt is uploaded to Zipline using a server-side token.
4. The server sends the payment details and returned receipt URL to Google Sheets.
5. The backend reserves the chair by publishing the selected duration to MQTT.
6. The Raspberry Pi publishes a one-minute setup deadline followed by the paid-session deadline.
7. The website reads the retained state through `/api/session` and displays the same countdown.
8. When the massage timer ends, the Pi publishes `idle` so another payment can begin.

Secrets and webhook configuration live in `.env`, which is excluded by `.gitignore`. Do not place the Zipline token in browser code or commit `.env`.

### Running the payment website

On Windows, double-click:

```text
start-site.cmd
```

Keep the server window open while the website is in use. The site is served at:

```text
http://localhost:3003
```

Alternatively, run:

```bash
node server.mjs
```

### Linux Docker deployment for the website

The payment website/backend has a dedicated Linux image defined by `Dockerfile.web`. It does not contain Python, Yoctopuce, USB access, the OLED driver, or the Raspberry Pi chair controller. The existing `Dockerfile` and `compose.yaml` remain exclusively for the Pi.

Create the website environment file on the Linux server:

```bash
cp .env.example .env
nano .env
```

Set the production Zipline token and Google Sheets webhook in `.env`, then build and start only the website stack:

```bash
docker compose -f compose.web.yaml up -d --build
```

The default address is `http://SERVER_IP:3003`. Change `WEB_PORT` in `.env` if the host must expose a different port. Useful operations are:

```bash
docker compose -f compose.web.yaml ps
docker compose -f compose.web.yaml logs --follow payment-website
docker compose -f compose.web.yaml restart payment-website
docker compose -f compose.web.yaml down
```

The container runs as the unprivileged `node` user with a read-only filesystem, a small temporary filesystem, a health check, automatic restart, and no USB/device access. Put a TLS reverse proxy in front of port 3003 when the site is internet-facing.

### Website files

| File | Purpose |
| --- | --- |
| `index.html` | Payment flow and page content. |
| `styles.css` | Responsive layout, branding, and animations. |
| `script.js` | Session selection, validation, uploads, and screen transitions. |
| `server.mjs` | Static server, Zipline upload proxy, and Google Sheets logging. |
| `start-site.cmd` | Windows launcher. |
| `.env.example` | Required environment-variable template. |
| `Dockerfile.web` | Linux payment website/backend image. |
| `compose.web.yaml` | Standalone Linux website service. |
| `chair_controller.py` | Raspberry Pi MQTT state machine, timer authority, and OLED output. |
| `requirements.txt` | Raspberry Pi/Python dependencies. |
| `session_display_test.py` | Complete display-only demo using the production screens and transitions, without MQTT or the website. |

## MQTT session coordination

Broker: `minecraft.duniapalsu.com:1883`

Retained topic:

```text
surau/setia-eco-glades/massage-chair/1/session
```

The single-chair state progression is:

```text
idle -> processing -> setup -> active -> idle
```

- `idle`: the website may accept a new payment.
- `processing`: the backend has reserved the chair and is recording the receipt.
- `setup`: payment handling succeeded and the user has 60 seconds to get comfortable.
- `active`: the paid 10- or 20-minute massage timer is running.
- Final `idle`: published by the Pi when massage time ends, unlocking the next payment.

The retained JSON includes `sessionId`, `minutes`, `phaseEndsAt` (Unix milliseconds), `updatedAt`, and `source`. Messages use QoS 1. Retention lets the backend and Pi recover the latest state after reconnecting.

The browser does not connect directly to MQTT. It polls `GET /api/session`; the Node backend returns its cached MQTT state. Both the website and OLED calculate their countdown from the Pi-published absolute `phaseEndsAt` timestamp.

MQTT is anonymous by default. If authentication is enabled later, set `MQTT_USERNAME` and `MQTT_PASSWORD` in `.env`. `MQTT_URL` and `MQTT_TOPIC` can also be overridden there.

The backend watchdog checks the retained state every five seconds. `processing` automatically returns to `idle` after three minutes without progress. `setup` and `active` automatically return to `idle` if they remain more than 30 seconds past `phaseEndsAt`. These limits can be changed through `MQTT_PROCESSING_TIMEOUT_MS` and `MQTT_DEADLINE_GRACE_MS`.

### Shelly Plus 1PM chair power bridge

The Raspberry Pi controller switches the Shelly Plus 1PM relay through the
same broker. The default topic prefix is:

```text
surau/setia-eco-glades/massage-chair/1/shelly-1pm
```

These values may be overridden in the Pi's `.env.pi` file:

```dotenv
SHELLY_MQTT_PREFIX=surau/setia-eco-glades/massage-chair/1/shelly-1pm
SHELLY_SWITCH_ID=0
```

The controller sends MQTT RPC commands to `<prefix>/rpc` and logs the full command topic after MQTT confirms publication. It turns the relay
on during the 60-second `setup` state and the paid `active` massage state.
When the massage timer reaches `00:00`, it keeps the session occupied while it
turns power off for 10 seconds, back on for 15 seconds to reset the chair, and
then off again. Only after that reset does it publish `idle` for the next
customer. On an MQTT reconnect, it resends the command appropriate for the
current session state. Commands are QoS 1 and deliberately not retained,
preventing an old power-on command from being replayed.

## OLED display

Python experiments for displaying a payment QR code and massage-chair messaging on a Yoctopuce **Yocto-MaxiDisplay YD128X64-A75A8** monochrome USB OLED.

The display resolution is **128 × 64 pixels**. The intended final system will run on a Raspberry Pi.

## Current display

The current static welcome screen divides the OLED into two 64 × 64 panels:

- Left: the original 64 × 64 payment QR code, kept at full resolution so it remains scannable.
- Right: `MASSAGE` and `CHAIR` in the built-in 8 × 8 pixel font, a 35 × 28 embedded massage-chair sprite, and `Scan QR to pay` in the small built-in font.

`welcome_display.py` contains the production welcome-screen renderer used by the MQTT controller. `welcome_test.py` remains as an earlier standalone copy.

## Files

| File | Purpose |
| --- | --- |
| `welcome_display.py` | Production static welcome-screen renderer used by the MQTT controller. |
| `welcome_test.py` | Working copy of the static welcome screen. |
| `animated_welcome_test.py` | Experimental spinning-chair animation with a fixed QR code. |
| `qrcode.bmp` | Original 64 × 64 payment QR image. |
| `massage_chair_clean.png` | Experimental cleaned chair source; the preferred sprite is embedded directly in the Python scripts. |

Files under `__pycache__` are generated by Python and are not source files.

## Requirements

- Python 3
- Pillow
- Yoctopuce Python library (`yoctopuce`), including the native ARM64 USB library
- Eclipse Paho MQTT client (`paho-mqtt`)
- Yoctopuce VirtualHub for the current Windows workflow, or direct USB access on Raspberry Pi/Linux

Install the Python packages in a virtual environment:

```bash
python -m pip install -r requirements.txt
```

The scripts use the Yoctopuce API package with native direct-USB support:

```python
from yoctopuce import yocto_api, yocto_display
```

## Running the static screen

With VirtualHub listening locally:

```bash
python welcome_display.py
```

The default hub address is `127.0.0.1`.

For direct USB access, including the planned Raspberry Pi setup:

```bash
python welcome_display.py --hub usb
```

Use another QR file if needed:

```bash
python welcome_display.py --qr path/to/qrcode.bmp
```

The current layout expects the QR image to be exactly 64 × 64 pixels. Do not resize this particular QR: reducing it damaged its module spacing and made it unreadable.

## Running the MQTT chair controller

The Raspberry Pi should run the controller instead of the static test:

```bash
python chair_controller.py --hub usb
```

Optional controller environment variables are `MQTT_HOST`, `MQTT_PORT`, `MQTT_TOPIC`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `SHELLY_MQTT_PREFIX`, and `SHELLY_SWITCH_ID`.

The controller shows the welcome QR while idle, shows payment processing, runs and publishes the 60-second setup countdown, runs the paid massage countdown, and finally publishes `idle`.

The active massage screen uses a boxed `MASSAGE TIMER` heading, a large countdown on the left, and the preferred massage-chair sprite on the right moving center, one pixel up-right, one pixel directly right of center, then center. The double-buffered animation is capped at 30 FPS so full frames reach the Yoctopuce display without overrunning its USB command queue.

When setup reaches `00:00`, `SET UP THE CHAIR` exits left inside its box while `MASSAGE TIMER` enters from the right. At the same time, `00:00` shifts left and the chair sprite enters from the right. The expired timer then rolls upward and the paid `10:00` or `20:00` timer rolls in from below. Paid time begins only after this transition finishes.

Processing-to-setup uses a synchronized push transition: processing scrolls through the top while setup enters from below. When massage ends, a rectangular black wipe closes from the outer edges into the center, then the idle QR welcome screen scrolls in from the bottom before MQTT returns the chair to `idle`.

When a new payment reserves the idle chair, an opaque box expands outward from the center of the QR welcome screen. Once it fills the OLED, the animated processing-payment screen appears.

The complete display sequence can be tested without MQTT:

```bash
python session_display_test.py --hub 127.0.0.1
```

It shows idle for 30 seconds, processing for 30 seconds, the 60-second setup countdown, the selected 10- or 20-minute massage timer, and the final idle screen for 30 seconds. Every production transition is included. Use `--minutes 20` to preview the longer session.

The paid duration and setup minute are separate. A 10-minute purchase occupies the chair for approximately 11 minutes total; a 20-minute purchase occupies it for approximately 21 minutes.

## Running the animation experiment

The animation test keeps the QR fixed on the left and spins the chair in the right panel using 24 pre-generated monochrome frames.

```bash
python animated_welcome_test.py
```

Choose a different frame rate:

```bash
python animated_welcome_test.py --fps 18
```

Run it over direct USB:

```bash
python animated_welcome_test.py --hub usb
```

Press `Ctrl+C` to stop the animation.

## Important discoveries

### `drawImage()` requires GIF data

The display reported:

```text
RCON:0083 GIF error: bad signature 42
```

Hexadecimal `42` is the first byte (`B`) of a BMP `BM` signature. On this firmware, `drawImage()` invokes the GIF decoder and cannot draw BMP bytes directly.

The working process is therefore:

1. Open the source image with Pillow.
2. Convert it to a one-bit image.
3. Save it as GIF in memory using `BytesIO`.
4. Upload the GIF bytes to the display.
5. Call `layer.drawImage()` using the uploaded `.gif` name.

The source QR file may remain a BMP; conversion happens in memory.

### The original partial-image problem was clipping

The first version drew a 64-pixel-high QR at `y=45` on a 64-pixel-high screen:

```python
layer.drawImage(x, 45, "qrcode.bmp")
```

Only 19 rows could remain visible, which looked like roughly one quarter of the image. A full-height image must start at `y=0`.

### Bitmap packing

Yoctopuce `drawBitmap()` expects packed one-bit scanlines:

- Rows run from top to bottom.
- Each byte represents eight horizontal pixels.
- The most-significant bit is the leftmost pixel.
- Row stride is `(width + 7) // 8` bytes.

Direct `drawBitmap()` experiments were less reliable in this setup, so the current scripts use the confirmed GIF upload and `drawImage()` route.

### Text alignment

`YDisplayLayer.ALIGN.CENTER` centers text around both the supplied `x` and `y` coordinates. Placing medium text at `y=0` clips its upper portion. Use the vertical center of the font rather than its intended top edge.

### QR sizing

The existing QR already uses the full 64-pixel display height. Scaling it down to fit below multiple lines of text made it unscannable. The chosen solution is a side-by-side layout that preserves the QR at 64 × 64.

## Current static coordinates

The right-hand panel is centered around `x=96`:

- `MASSAGE`: centered at `y=6`
- `CHAIR`: centered at `y=17`
- Chair sprite: top at `y=24`, height 28 pixels
- `Scan QR to pay`: centered at `y=58`

The title was moved upward by one pixel while the sprite and caption remained fixed.

## Raspberry Pi notes

Target deployment hardware:

- Raspberry Pi 5 Model B (64-bit ARM / `aarch64`)
- Debian 13 (Trixie)
- Yocto-MaxiDisplay connected by USB

### Docker deployment (recommended)

The Docker service runs only the MaxiDisplay MQTT controller. The payment
website, Zipline token, and Google Sheets webhook are not copied into or exposed
to this container.

Install Docker Engine and the Compose plugin on the Pi, copy this project to the
Pi, and change into the project directory. Confirm that Docker can see the USB
bus:

```bash
lsusb
ls -l /dev/bus/usb
```

The anonymous MQTT defaults already match the website, so no configuration file
is required. If MQTT authentication or another broker is needed, create a Pi
environment file:

```bash
cp .env.pi.example .env.pi
nano .env.pi
```

Build and start the controller. Pass `--env-file` only if `.env.pi` was created:

```bash
docker compose build
docker compose up -d

# Or, with custom MQTT settings:
docker compose --env-file .env.pi up -d --build
```

Watch the controller connect to the display and MQTT broker:

```bash
docker compose logs --follow maxidisplay-controller
```

Useful management commands:

```bash
docker compose ps
docker compose restart maxidisplay-controller
docker compose down
```

The service uses `restart: unless-stopped`, so it starts again after a Pi reboot
unless it was deliberately stopped. It mounts `/dev/bus/usb` because Linux USB
bus and device numbers may change when the MaxiDisplay is unplugged and
reconnected. The container runs as root so direct Yoctopuce USB access does not
depend on host udev user permissions.

If the log repeatedly says `No online display connected`, check `lsusb`, confirm
the cable carries data, and then restart the service. If Docker reports that
`/dev/bus/usb` does not exist, the Pi is not currently exposing any USB bus to
the host.

To update the controller after copying new source files onto the Pi:

```bash
docker compose up -d --build
```

The image is built locally on the Pi from the official multi-architecture Python
image, so Docker automatically selects its `linux/arm64` variant.

### Manual Python deployment

For a non-Docker Raspberry Pi deployment:

1. Use a supported Python 3 release.
2. Create a virtual environment and install the dependencies from `requirements.txt`, including `Pillow` and the cross-platform `yoctopuce` package.
3. Connect the display by USB.
4. Start with direct access using `--hub usb`; VirtualHub is optional.
5. Confirm the Linux user has permission to access the USB device. Follow Yoctopuce's Linux USB/udev setup if access is denied.
6. Run the script from any directory. Asset paths are resolved relative to the script file, not the current working directory.

The MQTT controller redraws only when the session state or displayed second changes. The unrelated spinning-chair experiment is not part of the payment workflow.

## Project log

This README is a living record of the project. Keep it open-ended and update it whenever the scripts, display layout, assets, hardware setup, deployment approach, or technical findings change. Items documented here describe completed work or current behavior; they are not automatically planned tasks.

- **8 August 2026:** Cleared a legacy retained MQTT session that was stuck in `processing` with the retired five-minute duration. The chair topic was verified back in `idle` with the backend connected.
- **8 August 2026:** Added automatic MQTT recovery for invalid retained durations, abandoned processing reservations, and expired setup or active deadlines.
- **8 August 2026:** Added complete English, Bahasa Malaysia, and Arabic website modes with persistent language selection and Arabic right-to-left layout.
- **8 August 2026:** Added a separate production-oriented Linux Docker image and Compose stack for the payment website/backend without including the Raspberry Pi controller or OLED dependencies.
- **8 August 2026:** Fixed the Pi container's empty Shelly topic default, enabled the installed relay topic by default, and added MQTT publication confirmation and startup diagnostics for Shelly commands.
- **8 August 2026:** Moved the website/backend from port 3000 to port 3003 across npm, Windows launcher, Docker, health checks, and documentation.
- **9 August 2026:** Updated the website support contact to `+60 11-3797 4563` with a matching tap-to-call link.
