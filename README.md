# Surau Setia Eco Glades Massage Chair

This project is split into two independent Docker deployments.

## `website/`

The payment website and primary chair controller. It validates a receipt, logs
the payment, owns the session timer, publishes status over MQTT, and switches
the Shelly Plus 1PM relay directly. It continues to operate when the Raspberry
Pi or OLED is unavailable.

Copy `website/.env.example` to `website/.env`, configure the receipt and Google
Sheets settings, then run from the `website` directory:

```bash
docker compose up -d --build
```

## `pi-controller/`

An optional Raspberry Pi OLED display client. It only subscribes to the shared
MQTT session state and renders the existing welcome, processing, setup, and
massage screens. It never controls the relay or changes session state.

Run from the `pi-controller` directory:

```bash
docker compose up -d --build
```

The Pi needs USB access to the Yocto-MaxiDisplay. Its optional `.env.pi` file
contains MQTT connection settings only.

## Session lifecycle

```text
idle → processing → setup (60 seconds) → active (10 or 20 minutes) → reset → idle
```

The website backend is the sole authority for this lifecycle. It energizes the
relay for setup and active states, then resets the chair by powering it off for
10 seconds, on for 30 seconds, and off again before publishing `idle`.

`README.legacy.md` preserves the previous project notes and deployment guide.
