# Changelog

## 0.2.1

- Imported readings use the integration's exact mg/dL attribute (`value_mg_dl`) when it
  agrees with the state, instead of converting the rounded mmol/L back.
- "decreasing_fast" / "increasing_fast" and similar map to SingleDown / SingleUp.

## 0.2.0 (renamed)

- Renamed from Nightscout Bridge to Nightscout Lite (slug `nightscout_lite`), repository
  `ha-nightscout-lite`.

## 0.2.0

- Own SQLite database: the bridge is now a small Nightscout server, not only a view of a
  Home Assistant sensor.
- Writes on a separate port (1338) with its own secret: entries, treatments (POST, PUT
  upsert, DELETE by id), device status. Values are range-checked; repeats are de-duplicated.
- The Home Assistant sensor is optional and imported into the same database.
- Newest reading published to `sensor.nightscout_glucose` with trend and delta.
- `/pebble` for older watch faces; `status.json` answers without a secret but says nothing.

## 0.1.0

- First version: Nightscout v1 entries, status and verifyauth from a Home Assistant glucose
  sensor's history; API secret required for every read; read-only.
