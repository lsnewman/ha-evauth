# EVAuth

A Home Assistant app (formerly "add-on") that works out **which car is in a carport** and
whether it may charge, replacing a pile of helpers and automations with one place that
logs every decision.

It combines Frigate licence-plate reads, Frigate's object classifier, a GPS device tracker,
a zone occupancy sensor and a tag scan, and books charging energy **per car**.

> **Status: early.** Sessions, the panel, the latching engine in **shadow mode** beside your existing
> automations, and charging control (scan flow and charger Availability) are built. Charging control is
> **off** until you enable it; until then the app touches nothing. See [docs/design.md](docs/design.md).

## What it does today

- **Latching engine** (pure and tested): LPR, classifier, GPS arrival/leave and zone-empty
  rules, with a configurable policy per source (tier, who may overwrite whom, staleness).
- **Shadow mode:** compares its latch with your existing HA latch and records every
  disagreement for you to review.
- **Charging control:** an NFC scan, car picker and timeouts, switching charger Availability, with a dry-run
  mode and a Home Assistant watchdog that fails closed.
- **Sessions:** recorded read-only from the charger entities, or added by hand; energy is
  booked to the car resolved at session end and can be reallocated.
- **Panel:** every setting is edited in the panel (no app options, nothing to migrate).

Fixed in code, not configurable: an Unknown read never overwrites a known latch, and the
classifier alone never authorises charging.

## Install

Add this repository in **Settings → Apps → App store → ⋮ → Repositories**:
`https://github.com/lsnewman/ha-evauth`, install **EVAuth**, start it, then open **EVAuth**
in the sidebar. Needs the Mosquitto broker app (or any broker Frigate publishes to).

## Develop

```bash
python3 -m venv .venv && .venv/bin/pip install pytest -r evauth/requirements.txt
cd evauth && ../.venv/bin/python -m pytest tests          # acceptance + invariant tests
EVAUTH_DEV=1 ../.venv/bin/python -m app.main              # panel on :8099 with fake HA/MQTT
python scripts/make_fixtures.py                           # regenerate the synthetic payloads
```

Dev mode adds `/api/dev/*` routes to inject Frigate payloads, entity states and time.

All fixtures and defaults are synthetic. **Never commit real plates, coordinates, tag IDs or
entity names.** `scripts/check_sensitive.sh` (a pre-commit hook; enable with
`git config core.hooksPath .githooks`) checks staged files against a private denylist.

## Licence

MIT
