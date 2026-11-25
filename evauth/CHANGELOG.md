# Changelog

Bump `version` in `config.yaml` with every release, or Home Assistant will not offer the update.

## 0.2.0b1

Charging control. It is **off** after updating, so nothing changes until you enable it on the Status page.

- **Scan flow:** a tag scan turns charger Availability on and, if nothing authorises yet, sends a car picker.
  No answer in 90 s authorises the car already identified by the classifier, else your default car.
- **Availability** turns on for a scan or a newly authorised latch, and off when nothing authorises, no
  10 minute window is open and nothing is plugged in.
- **Dry run** and **Live** modes. Live needs a watchdog automation and a heartbeat over MQTT.
- Setup: select the Availability switch (Sources), the NFC tag and watchdog (Settings), and give each user a
  default car (Users).
- The "counts as valid latch" setting now reads "authorises charging (turns Availability on)".
- A plate read by a non-authorising source no longer takes authority away from a scan.
- Shadow rows caused by the scan flow are marked intended automatically.
- Fixed: the Home Assistant connection reused message ids for keep-alive pings, and did not notice a silent
  connection.

## 0.1.0b4

- **Restarts:** after the app restarted, the shadow page briefly reported the app's latch as "none" (a false
  disagreement that closed itself at the next decision). It now starts from the latch it restored from disk.
- **Restarts and sensor drop-outs no longer count as a car leaving.** The first zone-occupancy reading after a
  restart, or "off" after the sensor was unavailable, used to start the 5 minute clear timer, which would clear
  a car that has no GPS (the old automation never saw an on-to-off change there). Such a latch is now
  "unconfirmed": the app waits a settle time (3 minutes by default, so Frigate can re-detect the car), then applies
  the normal 5 minute clear. If the sensor reads on again in that time, nothing clears. A real on-to-off still
  clears in 5 minutes. GPS and plugged-in protection apply as before.
- A latch made while the zone reads empty starts its own 5 minute clear clock.
- Routine "already latched" confirmations moved to the debug log; the log says when it connects to Home Assistant.

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
