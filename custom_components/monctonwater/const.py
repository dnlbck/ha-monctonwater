"""Constants for the Moncton Water integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "monctonwater"

BASE_URL = "https://myaccount.moncton.ca"

CONF_SCAN_INTERVAL = "scan_interval"
CONF_BACKFILL_DAILY = "backfill_daily"

# How many trailing days the per-refresh hourly continuation re-imports
# (heals the recorder's lumpy native rows and any compaction rewrites).
REIMPORT_DAYS = 2

# Smart meter readings publish once per day (yesterday's usage appears
# within 24 h), so polling more often than this is wasteful.
DEFAULT_SCAN_INTERVAL = timedelta(hours=4)
MIN_SCAN_INTERVAL = timedelta(minutes=30)

# The portal's usage pages document a 90-day window per query; the client
# pages back through windows of this size during the backfill.
QUERY_WINDOW_DAYS = 90

# How far back the background daily backfill walks before stopping, even if
# data continues (keeps the request count bounded).
BACKFILL_MAX_DAYS = 3 * 365

# Pause between paged backfill requests, to stay polite (the portal
# timed out occasional requests when paced at 1 s during testing).
BACKFILL_REQUEST_PAUSE = 2.0

STORAGE_KEY = "monctonwater"
STORAGE_VERSION = 1

# Bump to force one clean re-import of all statistics after changes to
# the import convention (v0.1 wrote per-period sums; the dashboard
# renders cumulative sums).
STATS_GEN = 5
