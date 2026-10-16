# Nightscout Lite

A minimal Nightscout-compatible server inside Home Assistant (not affiliated with the Nightscout Foundation). Uploaders (JugglucoNG, xDrip+) write to it,
followers read from it, and the newest reading is kept on a Home Assistant entity, so the
history is in Home Assistant as well, whichever sensor you wear.

Readings can also come from a glucose sensor Home Assistant already has, for example from the
LibreView integration. Both kinds land in the same database, so switching sensors (say, from
Libre to Syai uploaded by JugglucoNG) keeps one continuous history.

## Two ports, two secrets

| Port | Can | Secret | Meant for |
| --- | --- | --- | --- |
| `1337` | read only | `api_secret` (or `write_secret`) | followers; safe to publish with Tailscale Funnel |
| `1338` | read and write | `write_secret` only | uploaders; keep it on your network or tailnet |

A leaked read secret therefore cannot put a value into the data people follow. Leave
`write_secret` empty and port 1338 does not open at all.

Writes are checked before they are stored: glucose within 10–1000 mg/dL, insulin within
0–100 U, carbs within 0–1000 g, nothing dated in the future. A request with one bad document
is refused whole, with the reason in the answer.

## Configuration

| Option | Meaning |
| --- | --- |
| `api_secret` | Read secret, at least 12 characters. The one you give followers. |
| `write_secret` | Write secret, at least 12 characters, different from the read secret. Empty turns writes off. |
| `source_entity_id` | Optional existing glucose sensor to import, e.g. `sensor.libreview_glucose`. |
| `timestamp_attribute`, `trend_attribute`, `trend_entity_id` | Where the source sensor keeps its measurement time and trend. Empty tries the usual names (`timestamp`, `measurement_time`, `FactoryTimestamp`, `trend`, …). |
| `import_days` | How far back to import the source sensor on first start. |
| `publish_entity_id` | Entity kept at the newest reading (default `sensor.nightscout_glucose`). Empty turns it off. |
| `display_units` | `mg/dl` or `mmol`, for that entity and for `status.json`. Entries are always mg/dL. |
| `keep_days` | Delete readings older than this; `0` keeps everything. |

The database is `/data/nightscout.sqlite3` and is part of the add-on's backups.

## How readings get from the source sensor here

The add-on keeps one idle websocket to Home Assistant, subscribed to state changes of the
source sensor only. When the sensor changes, Home Assistant sends that one state and the
add-on stores it about a second later; followers see it on their next poll.

As a safety net it also reads the sensor's history from Home Assistant's database every 5
minutes, which fills anything missed while the add-on or the websocket was down. If the
websocket cannot be kept up, that history read runs every minute instead. On first start it
imports `import_days` of history, half a day per request.

## The Home Assistant entity

`sensor.nightscout_glucose` gets each new reading within seconds, with `measured_at`,
`direction`, `trend_arrow` and `delta` as attributes. It has `state_class: measurement`, so
Home Assistant also keeps long-term statistics for it.

Home Assistant records a state at the moment it is set. Readings uploaded late (a phone that
was offline catching up) are stored in the add-on's database with their real time, but are not
sent to the entity if they are older than 15 minutes: they would be recorded at the wrong time.

## Clients

**JugglucoNG uploading:** Nightscout → Upload, URL `http://<home-assistant>:1338`, secret
`write_secret`, v3 off. Glucose, journal treatments (including edits and deletes) and device
status are supported.

**JugglucoNG following:** Nightscout → Follow, URL `http://<home-assistant>:1337` or the
Funnel address, secret `api_secret`, v3 off. Do not upload from a phone that follows this same
server.

**xDrip+:** Nightscout follower or REST uploader with `https://<api_secret or write_secret>@<host>/api/v1/`, using the port that fits.

**Watch faces and other followers:** `/api/v1/entries`, `/api/v1/entries/sgv`,
`/api/v1/entries/current`, `/api/v2/properties` and `/pebble` are served.

## Publishing with Tailscale Funnel

Publish only the read port, never 1338 and never Home Assistant's 8123:

```sh
tailscale funnel --bg --https=443 http://127.0.0.1:1337
```

The followers' URL is then `https://<machine>.<tailnet>.ts.net`. Use a long random read
secret (`openssl rand -base64 24`), and remember that a power or internet outage at home takes
the followers down with it.

## Not included

* Nightscout's web pages (charts, reports). Home Assistant dashboards and the client apps
  take their place.
* API v3 and its websocket, so AndroidAPS cannot use this server.
* Profiles and food.
