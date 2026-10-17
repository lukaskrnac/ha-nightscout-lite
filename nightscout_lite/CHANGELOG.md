# Changelog

## 0.3.1

- A follower poll that would bring nothing new (the client already has the newest reading) is
  held until the next reading arrives, up to `hold_seconds` (20 by default). JugglucoNG
  schedules its next poll a fixed interval after the previous one finished, so its polls then
  line up just after each reading instead of landing up to a minute late. Clients the add-on
  cannot tell are up to date are answered at once, as before.

## 0.3.0

- Readings from the source sensor are pushed: the add-on keeps one idle websocket to Home
  Assistant, subscribed to that sensor only, and stores each change about a second after
  Home Assistant has it (was: up to a minute).
- With the subscription up, the history read becomes a safety net every 5 minutes instead of
  every minute, so Home Assistant does less work than before. If the websocket drops, reading
  history every minute resumes until it reconnects.

## 0.2.3

- Fix: the import asked Home Assistant for history without an end time, which Home Assistant
  answers with one day only, so the first import stopped after its first day. History is now
  read in half-day windows up to now.
- Fix: import progress was remembered as the newest measurement time found. A sensor showing a
  stale reading (measured days earlier) pulled every later import back to the same window, and
  no new readings arrived. Progress is now the end of the last window read.

## 0.2.2

- Fix: the Supervisor token was not found under s6-overlay, so importing from the source
  sensor and publishing the entity were off. It is now read from the container environment
  (and the program starts through `with-contenv` when the image has it).
- A client disconnecting mid-request (the Supervisor's port check) no longer logs a traceback.

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
