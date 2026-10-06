# Moncton Water for Home Assistant

An unofficial Home Assistant custom integration that pulls water usage
from the City of Moncton's [MyAccount
portal](https://myaccount.moncton.ca) (Moncton, New Brunswick, Canada)
and feeds it into the **Energy dashboard's water section**.

The City publishes no official API. This integration signs in to
MyAccount with your username and password, exactly like a browser does,
and scrapes the same server-rendered pages the portal shows — the billed
consumption table and the smart meter's daily usage chart. Sessions are
short-lived; the integration logs in again automatically whenever the
portal expires one. The wire format was reverse-engineered against live
traffic in September 2026.

## Features

- Username/password sign-in with automatic session renewal
- **Energy dashboard ready**: a cumulative `total_increasing` water
  sensor (m³) derived from the portal's own books — all billed periods
  plus the current period's daily readings
- **~3 years of history backfill**: on first setup, every billed period
  (~13 quarters) is imported as long-term statistics — each period's m³
  spread evenly across its days
- **Smart-meter upgrade**: a background task then re-fetches the meter's
  full daily history (back to its activation, September 2024 on the
  test account) in 90-day windows and replaces the spread estimates
  with real daily readings — about ten requests, runs once
- **Hourly resolution**: a second background walk pulls the portal's CSV
  export (hourly values per day) in 90-day windows and upgrades the
  whole smart-meter era to hourly rows; each refresh keeps new days
  hourly. Imported statistics use cumulative sums — the convention the
  Energy dashboard's rendering requires — so consumption never renders
  negative
- Latest daily usage, latest billed amount, trailing-window daily
  average; account/meter/address attributes

Each refresh reads one page, the smart meter's daily window; the billed
table, which only changes quarterly, is re-read once a day. Daily
readings publish once per day — yesterday's usage appears within 24
hours, sometimes in stages, and recent days are re-imported when the
portal revises them.

## Entities

| Entity | Class | Description |
|---|---|---|
| `sensor.moncton_water_water_usage` | water (m³, total increasing) | **Use this one in the Energy dashboard.** Cumulative counter derived from billed periods + daily readings. |
| `sensor.moncton_water_last_daily_water` | water (m³) | Most recent daily consumption |
| `sensor.moncton_water_last_billed_water` | water (m³) | Most recent billed period's consumption |
| `sensor.moncton_water_daily_average_water` | water (m³) | Average per day over the fetched window |

The water usage sensor also exposes `account_number`, `meter_id`,
`service_address`, `last_daily_date`, `last_billed_date`, and `daily_m3`
(the trailing window) as attributes.

## Installation

Requires Home Assistant 2025.11 or newer.

### HACS

1. HACS → ⋮ → **Custom repositories**
2. Add this repository, category **Integration**
3. Install **Moncton Water**, restart Home Assistant

### Manual

Copy `custom_components/monctonwater/` into the `custom_components/`
directory of your Home Assistant configuration and restart.

## Configuration

1. **Settings → Devices & Services → Add Integration → Moncton Water**
2. Enter your Moncton MyAccount username and password
3. First setup imports the billed history as statistics; the
   smart-meter daily upgrade then runs in the background (a few
   minutes)

### Adding to the Energy dashboard

1. **Settings → Dashboards → Energy**
2. Under **Water consumption**, click **Add consumption**
3. Select **Moncton Water Water usage**

### Data resolution & freshness

- Daily smart-meter data is published once per day; yesterday's usage
  appears within 24 hours.
- The billed table carries ~13 quarterly periods (~3 years). Before the
  meter's activation (September 2024 on the test account) those periods
  are imported as evenly-spread daily averages — daily views for those
  quarters are estimates, while quarter/year views total exactly. From
  the activation date on, the Energy dashboard shows real daily values,
  and after the hourly backfill completes, real hourly values.

### Options

The polling interval (1–24 hours, default 4) and the one-time
hourly-resolution backfill (on by default) can be changed via
**Configure** on the integration entry.

## Verifying the API before installing

To exercise the client against the live portal without Home Assistant:

```bash
python -m venv .venv
.venv/Scripts/pip install aiohttp   # Windows
set MONCTONWATER_USERNAME=<your username>
set MONCTONWATER_PASSWORD=<your password>
.venv/Scripts/python scripts/test_client.py
```

It logs in, prints the account, every billed period, the trailing daily
window, walks the full daily history in 90-day windows, checks
yesterday's hourly data, and writes raw HTML captures to `scripts/out/`
(gitignored — they contain your personal usage data).
`MONCTONWATER_COOKIES=JSESSIONID=...; capricornWCMid=...` seeds an
existing browser session instead of logging in.

## Development

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements_test.txt pytest-homeassistant-custom-component
# Windows: POSIX module shims + the repo root are needed for the HA test plugin
PYTHONPATH="$PWD/tests/windows_stubs" .venv/Scripts/python -m pytest
```

The test suite runs the client against a mock portal server (which
reproduces the real wire format, down to the quirk where a to-date of
today makes the portal serve its default 30-day window) and full Home
Assistant setup/config-flow/statistics tests
(`pytest-homeassistant-custom-component`). Parser tests run against
captured real-portal markup in `tests/fixtures/`.

## Caveats

- **Unofficial.** The City of Moncton publishes no API and can change
  the portal at any time. Use a reasonable polling interval (the
  default 4 h is generous; the data updates about daily).
- Multi-account profiles: the portal's *active* account is used.
  Separate MyAccount logins can each be added as their own entry.
- This project is not affiliated with the City of Moncton.

## License

MIT — see [LICENSE](LICENSE).
