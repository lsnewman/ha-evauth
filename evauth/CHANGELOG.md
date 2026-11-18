# Changelog

Bump `version` in `config.yaml` with every release, or Home Assistant will not offer the update.

## 0.1.0b2

- Shadow mode now adopts the existing HA latch whenever it can be mapped to a configured car, so the order
  you fill in Sources and Cars no longer matters. Before, saving the latch helpers before the cars left a
  false "disagreeing" row.
- If that row is already there: mark it explained, or ignore it. It closes itself once the two sides agree.

## 0.1.0b1

- First build: panel, settings store, session store, latching engine, shadow comparison.
- Observe-only. The scan flow is tested as a pure state machine but not connected to anything.
