# Nightscout Lite for Home Assistant

A Home Assistant add-on that is a minimal Nightscout-compatible server: uploaders write to it, followers
read from it, and the newest reading is kept on a Home Assistant entity so the history is in
Home Assistant too. It can also import a glucose sensor Home Assistant already has (for
example from the LibreView integration).

## Install

1. Settings → Add-ons → Add-on store → ⋮ → **Repositories**, add
   `https://github.com/lukaskrnac/ha-nightscout-lite`.
2. Install **Nightscout Lite**, set a read secret and (for uploads) a write secret, start it.
3. Uploaders use port 1338 with the write secret; followers use port 1337 with the read secret.

See [nightscout_lite/DOCS.md](nightscout_lite/DOCS.md) for options and for publishing it
with Tailscale Funnel.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The add-on uses only Python's standard library.

Not affiliated with the Nightscout Foundation; it speaks the Nightscout API so Nightscout clients work with it.
