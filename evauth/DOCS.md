# EVAuth

Works out which car is in the carport, decides whether it may charge, and keeps a per-car energy
ledger. **Charging control is off until you enable it**: until then the app only reads Frigate and
Home Assistant and compares its decisions with your existing latch (shadow mode, which keeps running
in every mode).

There is nothing to configure on the Configuration tab. Open the **EVAuth** panel (admins only).

## First run

1. **Sources:** (set the charger status to the *connector* status sensor, not the charger-level one)
    the Frigate camera, zone, and label; the zone occupancy sensor; the helpers of
   your existing latch (for comparison); optionally the charger status and session-energy sensors.
2. **Cars:** name, plate patterns, Frigate class, GPS tracker, opening kWh.
   Patterns ignore punctuation and case: `*` any run of characters, `?` one character, `[QDO0]` one of a set.
3. **Users:** the phone's HA device id, notify target, default car, allowed flag.
4. **Tiers & timings:** defaults reproduce the old automations.

## Concepts

- **Latch:** the current car, the source that set it, and when. One at a time; history is kept.
- **Sources:** `lpr`, `gps`, `classifier`, `nfc`, `prompt`, `default`. Each has a tier
  (confident / non-confident), a minimum score, what it may overwrite, and whether it may authorise.
- **Authorise** means "counts as a valid latch": it turns charger Availability on and keeps it on.
  The classifier never authorises.
- **Unknown** is a built-in car with no plates. It can only fill an empty latch.

## Charging control

Before enabling it:

1. **Sources:** select the charger **Availability** switch.
2. **Users:** each user needs a phone, a notify target and a default car.
3. **Settings → Charging control:** select the NFC tag on the charger.
4. Turn off any automation that already handles that tag, so you are not asked twice.

Then choose a mode on the **Status** page:

- **Dry run:** scan the tag for real and answer the notifications. Nothing is switched; the page shows what
  would have happened. Check that your phone is recognised.
- **Live:** needs the watchdog automation (see the project's `docs/design.md`), selected under Settings.

When live:

- Availability is **off** whenever nothing authorises (many chargers flash a light while unavailable).
- It turns on when you scan, or when an authorising source (by default a plate read or GPS) latches a car.
- Availability must be on **before** plugging in. If the car was already plugged in, unplug and plug it in again.
- After a scan, or after you turn the switch on yourself, it stays on for 10 minutes without a latch.
- A plugged-in or charging car is never switched off.
- Switching to Off or Dry run turns Availability back on.

## Shadow page

Every period where the app and the HA latch differ is one row. Mark each *explained*,
*intended* or *bug*. Rows caused by the app's own scan flow are marked *intended* automatically, because
your existing helpers never hear of it.

## Settings storage

Everything lives in `/data/config.json` (atomic saves, previous copy kept as `.bak`, upgraded
automatically between versions). Events, sessions and disagreements are in `/data/evauth.db`.
