# EVAuth

Works out which car is in the carport and keeps a per-car energy ledger. This version
runs in **shadow mode**: it reads Frigate and Home Assistant, makes its own decisions,
and compares them with your existing latch. It writes nothing to Home Assistant.

There is nothing to configure on the Configuration tab. Open the **EVAuth** panel (admins only).

## First run

1. **Sources:** the Frigate camera, zone, and label; the zone occupancy sensor; the helpers of
   your existing latch (for comparison); optionally the charger status and session-energy sensors.
2. **Cars:** name, plate patterns, Frigate class, GPS tracker, opening kWh.
   Patterns ignore punctuation and case: `*` any run of characters, `?` one character, `[QDO0]` one of a set.
3. **Users:** the phone's HA device id, notify target, default car, allowed flag.
4. **Tiers & timings:** defaults reproduce the old automations.

## Concepts

- **Latch:** the current car, the source that set it, and when. One at a time; history is kept.
- **Sources:** `lpr`, `gps`, `classifier`, `nfc`, `prompt`, `default`. Each has a tier
  (confident / non-confident), a minimum score, what it may overwrite, and whether it may authorise.
- **Authorise** means "counts as a valid latch, so charging may go ahead". In shadow mode this is only displayed.
- **Unknown** is a built-in car with no plates. It can only fill an empty latch.

## Shadow page

Every period where the app and the HA latch differ is one row. Mark each *explained*,
*intended* or *bug*. Cutting over is only sensible when none is open.

## Settings storage

Everything lives in `/data/config.json` (atomic saves, previous copy kept as `.bak`, upgraded
automatically between versions). Events, sessions and disagreements are in `/data/evauth.db`.
