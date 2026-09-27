"""Restart Home Assistant and verify the monctonwater integration loads.

Mirrors the deploy flow from the homeassistant-NB project: POST the
restart service with the long-lived token from ../homeassistant-NB/.ha_token
(or MONCTONWATER_HA_TOKEN), poll until the API answers again, then scan
the error log for monctonwater entries.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error

HA_URL = os.environ.get("MONCTONWATER_HA_URL", "http://homeassistant.local:8123")


def token() -> str:
    env = os.environ.get("MONCTONWATER_HA_TOKEN")
    if env:
        return env
    for path in (
        r"C:\Sources\homeassistant-NB\.ha_token",
        os.path.expanduser("~/.ha_token"),
    ):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
    raise SystemExit("no HA token found (MONCTONWATER_HA_TOKEN or .ha_token)")


def api(path: str, method: str = "GET", data=None, timeout: int = 30):
    req = urllib.request.Request(
        f"{HA_URL}{path}",
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "Authorization": f"Bearer {token()}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def main() -> int:
    print("requesting restart...")
    req = urllib.request.Request(
        f"{HA_URL}/api/services/homeassistant/restart",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=30)
    except Exception:
        pass  # HA drops the connection as it restarts

    for attempt in range(60):
        try:
            config = api("/api/", timeout=5)
            print(f"HA is back (attempt {attempt + 1}): {config.get('message')}")
            break
        except Exception:
            time.sleep(5)
    else:
        print("HA did not come back within 5 minutes")
        return 1

    time.sleep(10)  # give integrations a moment to settle

    def show_log_entries() -> None:
        """Print monctonwater entries from HA's in-memory log."""
        try:
            import websocket  # type: ignore

            ws = websocket.create_connection(
                f"ws://{HA_URL.split('//', 1)[1]}/api/websocket", timeout=15
            )
            ws.recv()
            ws.send(json.dumps({"type": "auth", "access_token": token()}))
            json.loads(ws.recv())
            ws.send(json.dumps({"id": 1, "type": "system_log/list"}))
            logs = json.loads(ws.recv())["result"]
            ws.close()
        except Exception as err:  # noqa: BLE001
            print(f"(could not read system log: {err})")
            return
        hits = [e for e in logs if "monctonwater" in json.dumps(e).lower()]
        print(f"{len(hits)} monctonwater entries in the system log:")
        for entry in hits[-10:]:
            print(" ", entry.get("level"), str(entry.get("message"))[:300])

    show_log_entries()
    return 0


if __name__ == "__main__":
    sys.exit(main())
