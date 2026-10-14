---
name: dohm-ble
repository: https://github.com/martinemde/dohm-ble
status: active
---

# Dohm BLE

Home Assistant custom integration and standalone BLE client for the Marpac
Dohm Connect white-noise machine. The library supports names, clock, schedules,
and experimental commands; HA exposes only power and speed/volume. See
`docs/library.md` for standalone use. Android app protocol research lives in
`docs/` with local APK and decompiler output in ignored `artifacts/`.

Run tests with `uv run pytest`. See `CLAUDE.md` for project guidelines.
