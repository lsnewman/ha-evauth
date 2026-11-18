# EVAuth design

EVAuth replaces carport EV latching and charge-authorisation logic that used to live in HA helpers
and automations. It is built in phases; only phases 1 and 2 exist in this release.

| Phase | What | State |
| --- | --- | --- |
| 1 | Session store, picker, car/user config. No effect on charging | built |
| 2 | Latching engine in shadow mode beside the HA automations | built |
| 3 | Review disagreements, cut over when each is explained | after weeks of shadow data |
| 4 | Scan flow: Availability on, car picker, 90 s default, 10 min no-plug-in rule | pure state machine + tests only |
| 5 | Retire the old helpers and automations | later |

## Fixed rules

1. Fail closed: if identity is uncertain, or the app is quiet, charger Availability should stay off.
2. The classifier alone never authorises. Whether LPR alone authorises is a setting (default: yes, a plate read is the strongest identification).
3. Stale latches clear fast.
4. An Unknown read never overwrites a known latch.
5. Energy is booked per car, at session end.
6. An unmapped phone authorises nothing.
7. Availability goes on at scan and back off if the charger has not left Available within 10 minutes with no valid latch.

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
6. The 90 s `default` source cannot overwrite a standing latch (phase 4).
7. The "leaves home" rule is unchanged (any non-home state clears, so carport to work clears); only the log wording is corrected.
8. An event log in SQLite replaces logbook lines and the observe-only automations.
9. Ledgers are derived from sessions inside the app; mirroring to HA `input_number` helpers is deferred.
10. **New:** when a confident source confirms the car already latched by a non-authorising source
    (for example GPS after the classifier), the latch's source is upgraded in place (`upgrade_same_car`).
    This will show as a *source* disagreement in shadow mode.
11. A repeated read of the same car by a source that refreshes (LPR) refreshes the latch time, as before.
12. On first start the engine adopts the HA latch once, so shadow comparison starts from the same state.

## Assumptions to verify against real payloads

- The classifier result arrives as the object's own `Car Identity` `[name, score]` at the top level of
  `before`/`after`. If your Frigate version nests it (for example under `attributes`), `frigate.py` needs a one-line change.
- `device_tracker` `in_zones` lists the carport zone entity id.

## Phase 4 sketch (not connected)

Scan -> user from the phone's device id -> Availability on -> picker (iOS actionable notification) ->
pick sets `prompt`; no pick in 90 s sets the user's default car (`default`) -> if still no valid latch and
no plug-in after 10 min, Availability off.

### Watchdog proposal (HA side, for your approval; nothing has been written)

The app will publish a heartbeat; a small HA automation turns Availability off when it goes quiet.
Entity names below are placeholders.

```yaml
alias: EVAuth watchdog
trigger:
  - platform: state
    entity_id: sensor.evauth_heartbeat
    to: unavailable
    for: "00:02:00"
  - platform: homeassistant
    event: start
action:
  - service: switch.turn_off
    target:
      entity_id: switch.charger_availability
```
