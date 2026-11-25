# EVAuth design

EVAuth replaces carport EV latching and charge-authorisation logic that used to live in HA helpers
and automations. It is built in phases.

| Phase | What | State |
| --- | --- | --- |
| 1 | Session store, picker, car/user config. No effect on charging | built |
| 2 | Latching engine in shadow mode beside the HA automations | built |
| 3 | Review disagreements, cut over when each is explained | shadow comparison carries on |
| 4 | Charging control: scan flow and charger Availability | built; off until enabled (dry run, then live) |
| 5 | Retire the old helpers and automations | later |

## Fixed rules

1. Fail closed: if identity is uncertain, or the app is quiet, charger Availability should stay off.
2. The classifier alone never authorises. Whether LPR alone authorises is a setting (default: yes, a plate read is the strongest identification).
3. Stale latches clear fast.
4. An Unknown read never overwrites a known latch.
5. Energy is booked per car, at session end.
6. An unmapped phone authorises nothing.
7. Availability goes on at scan and back off if the charger has not left Available within 10 minutes with no valid latch.
8. A running or plugged-in session is never disabled.

## Engine

`engine.py` is pure: signals and a clock go in, decisions come out, so every rule is tested without
sleeping. Per source, the policy says what the source may overwrite (empty / Unknown / fresh car /
stale car), whether it refreshes the same car, and whether it authorises. Defaults:

| Source | Over empty | Over Unknown | Over fresh car | Over stale car | Authorises |
| --- | --- | --- | --- | --- | --- |
| lpr | yes | yes | yes | yes | yes (setting) |
| gps | yes | yes | no | yes | yes |
| classifier | yes | yes | no | no | never |
| nfc / prompt | yes | yes | yes | yes | yes |
| default | yes | yes | no | no | yes |

Clears: zone empty for 5 min clears the latch unless the latched car has a GPS tracker still in the
zone (or unavailable); a tracker that leaves `home` for any other state (including a named zone) for 2 min
clears that car's latch. Unavailable states never trigger a clear.

## Deviations from the previous behaviour

Each is open for review.

1. A per-source policy matrix replaces one hard-coded policy; defaults match the old behaviour.
2. GPS is per car, not for one car only.
3. GPS replacing a stale latch is now the explicit `over_known_stale` setting for `gps` (300 s default,
   same as before). It was a side effect, not a design choice.
4. Occupancy `unavailable`/`unknown` means "no change", never a clear.
5. Sources `elimination` and `plug` are dropped.
6. The 90 s `default` source cannot overwrite a standing known car (see 16).
7. The "leaves home" rule is unchanged (any non-home state clears, so carport to work clears); only the log wording is corrected.
8. An event log in SQLite replaces logbook lines and the observe-only automations.
9. Ledgers are derived from sessions inside the app; mirroring to HA `input_number` helpers is deferred.
10. **New:** when a confident source confirms the car already latched by a non-authorising source
    (for example GPS after the classifier), the latch's source is upgraded in place (`upgrade_same_car`).
    This will show as a *source* disagreement in shadow mode.
11. A repeated read of the same car by a source that refreshes (LPR) refreshes the latch time, as before.
12. On first start the engine adopts the HA latch once, so shadow comparison starts from the same state.
13. **New:** a car plugged in or charging keeps its latch. The old automations clear on an empty zone alone, which
    a flaky detection can trigger while a car is charging. The connector states Preparing, Charging, SuspendedEV,
    SuspendedEVSE and Finishing count as "plugged in" (a car that is plugged in is certainly there). The
    zone-empty and GPS-left-home clears wait, and restart their timers when the plug-in ends. Shadow mode will
    show this as *car* disagreements whenever the old automations clear a plugged-in car; they are intended.
    Setting: `charger_holds_latch`.
14. **New:** a latch that cannot be confirmed (first occupancy reading after a restart, or the sensor lost and
    back) is not cleared by the 5 minute rule, because that was never a departure. It gets a settle time
    (`restart_grace_s`, default 3 min, for Frigate to re-detect the car) and then the normal clear time. The old automation has the same blind spot, with no timer at all.
15. **New:** a latch made while the zone reads empty starts a fresh clear clock.

16. **New:** if a scan goes unanswered and a known car is already latched without authority (the classifier),
    the scan authorises that car, as source `nfc`. Otherwise the user's default car is used. The old
    automation overwrote any latch with the default.
17. **New:** a non-authorising source seeing the authorised car again refreshes the latch time but keeps the
    authorising source, so a plate read cannot revoke a scan when LPR is set not to authorise.
18. Scan latches (`nfc`, `prompt`, `default`) never reach the old helpers, so shadow rows they cause are
    recorded as *intended* automatically.

## Assumptions to verify against real payloads

- The classifier result arrives as the object's own `Car Identity` `[name, score]` at the top level of
  `before`/`after`. If your Frigate version nests it (for example under `attributes`), `frigate.py` needs a one-line change.
- `device_tracker` `in_zones` lists the carport zone entity id.
- The `device_id` on a tag scan is the device-registry id chosen on the Users page.
- A tap on an actionable notification arrives as `mobile_app_notification_action` with the button's `action`.
- The Availability switch reports its new state within 30 seconds of a command.
- Turning the switch off and on around a charger or Home Assistant restart does not look like manual use
  (it passes through unavailable).

## Charging control (phase 4)

**The app acts; Home Assistant watches.** The app hears the tag scan and the notification tap, maps the phone
to a user, runs the timers, switches charger Availability and sends the messages, so there is one tested,
restart-safe state machine (`charging.py`, pure). Home Assistant keeps only what must work when the app is
dead: the watchdog.

| Mode | Scans | Notifications | Switch |
| --- | --- | --- | --- |
| off (default) | ignored | none | untouched |
| dry run | handled | sent, marked `[dry run]` | commands only logged, against a simulated switch |
| live | handled | sent | switched |

Live can only be selected when the switch, connector sensor, tag, a complete user, Home Assistant, MQTT and
the watchdog are all in place. Leaving live turns Availability on once.

**On** (events): a scan by an allowed user; a latch becoming authorised (which sources authorise is the per-source
setting); after a restart or reconnect, an authorised latch with an occupied zone. A manual off is respected
until the next such event.

**Off** (condition): nothing authorises, no scan is waiting, no plug-in window is open, and the connector is
known to have nothing plugged in. The window (10 minutes) opens on a scan, and when someone else turns the
switch on. An unconfirmed command is sent three times, then reported to the owner and repeated every 10 minutes.

**Scan:** unmapped or disallowed phone: owner told, nothing changes. Otherwise Availability on; if nothing
authorises yet, a notification with one button per car. Tap: `prompt`. No answer in 90 s: see deviation 16.

### Watchdog

The app publishes `sensor.evauth_heartbeat` through MQTT discovery every 30 s while it is connected to Home
Assistant (from the first time charging control is enabled; it carries on if control is switched off again).
The entity expires after the watchdog timeout (default 120 s). This automation lives in Home Assistant; replace
the placeholder entity names. It acts only with nothing plugged in, about 4 minutes at most after the app dies.

```yaml
alias: EVAuth watchdog
triggers:
  - trigger: time_pattern
    minutes: "/1"
conditions:
  - condition: state
    entity_id: sensor.evauth_heartbeat
    state: [unavailable, unknown]
    for: "00:01:00"
  - condition: state
    entity_id: switch.my_charger_availability
    state: "on"
  - condition: state
    entity_id: sensor.my_charger_status_connector
    state: Available
actions:
  - action: switch.turn_off
    target:
      entity_id: switch.my_charger_availability
  - action: notify.mobile_app_your_phone
    data:
      message: EVAuth is not responding; charger Availability turned off.
```

**Rollback:** set the mode to off, disable the watchdog, and re-enable whatever handled the tag before.
