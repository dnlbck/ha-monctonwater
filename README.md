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
- **Energy dashboard ready**: a water usage statistic, *Moncton Water
  usage (your account number)*, built and kept current by the
  integration:
  - **~3 years of history**: the billed periods before your smart meter
    (~13 quarters), each spread evenly across its days
  - **Hourly resolution** for the last two years (the portal keeps 730
    days of meter data), from its CSV export; hourly days stay in the
    statistic after the portal drops them
  - each refresh appends newly published days and catches up after
    downtime; the latest days are imported again until the portal stops
    revising them
- A cumulative `total_increasing` water sensor (m³) — all billed periods
  plus the current period's daily readings — for automations and history
- Latest daily usage, latest billed amount, trailing-window daily
  average; account/meter/address attributes

Usage arrives a day late: the portal publishes yesterday within 24
hours, sometimes in stages. That is why the history lives in a statistic
of its own rather than the sensor's — Home Assistant builds a sensor's
statistics from its live state, so it would book each day in one lump
when the portal publishes it.

Each refresh reads one page, the smart meter's daily window; the billed
table, which only changes quarterly, is re-read once a day.

## Entities

| Entity | Class | Description |
|---|---|---|
| `sensor.moncton_water_water_usage` | water (m³, total increasing) | Cumulative counter derived from billed periods + daily readings. |
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
3. The usage history imports in the background (a few minutes)

### Adding to the Energy dashboard

1. **Settings → Dashboards → Energy**
2. Under **Water consumption**, click **Add consumption**
3. Select **Moncton Water usage (*your account number*)**

Today stays empty until the portal publishes it, the next day.

### Data resolution

- The last two years: hourly, as the meter recorded it. The portal keeps
  730 days of meter data, so that is how far a first import reaches;
  days stay hourly in the statistic after the portal drops them.
- Before that, from the billed table (~13 quarterly periods, ~3 years;
  the integration keeps quarters that later drop off the table): each
  period spread evenly across its days, so quarter and year views total
  exactly while daily views of those quarters are estimates.
- Accounts without smart-meter data get the billed periods only, the
  statistic growing as each new period bills.

### Options

The polling interval (1–24 hours, default 4) and the history backfill
(on by default; off starts the statistic with the last few days) can be
changed via **Configure** on the integration entry.

### Starting the history over

Run the **Moncton Water: Rebuild usage history** action (Developer
tools → Actions, `monctonwater.rebuild_history`): it clears the usage
statistic and imports it again from the portal, in a few minutes. Days
older than the portal's two years of meter data come back as billed
averages, so rebuild only when needed.

### Upgrading from 0.3

Earlier versions imported the history into the water sensor's own
statistic, where it could not line up with the sensor's live data (the
Energy dashboard's *today* showed usage that had not happened yet).
From 0.4 the history goes to the separate statistic instead:

1. In the Energy dashboard, replace the water source **Moncton Water
   Water usage** with **Moncton Water usage (*your account number*)**.
   The sensor keeps working, but its statistic no longer gets history.
2. The sensor's statistic keeps what 0.3 imported. Nothing reads it once
   the Energy dashboard is switched, so it can stay.

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
