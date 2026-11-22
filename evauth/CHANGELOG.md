# Changelog

Bump `version` in `config.yaml` with every release, or Home Assistant will not offer the update.

## 0.1.0b3

**Upgrading:** set **Charger status** on the Sources page to the *connector* status sensor (for OCPP chargers
this is usually `sensor.<charger>_status_connector`). The charger-level status sensor never leaves
"Available", so sessions were never recorded.

- **A car that is plugged in or charging keeps its latch.** While the connector reads Preparing, Charging,
  SuspendedEV, SuspendedEVSE or Finishing, the zone-empty clear and the GPS-left-home clear wait (logged once).
  When the plug-in ends, a fresh timer starts. A setting turns this off (Tiers & timings).
- Sessions added by hand now have a start and an end time, and session times can be edited in the table.
  A hand-added session needs both, so it can never block the charger's own session recording.
- The Status page shows whether a car is plugged in.

## 0.1.0b2

- Shadow mode now adopts the existing HA latch whenever it can be mapped to a configured car, so the order
  you fill in Sources and Cars no longer matters. Before, saving the latch helpers before the cars left a
  false "disagreeing" row.
- If that row is already there: mark it explained, or ignore it. It closes itself once the two sides agree.

## 0.1.0b1

- First build: panel, settings store, session store, latching engine, shadow comparison.
- Observe-only. The scan flow is tested as a pure state machine but not connected to anything.
