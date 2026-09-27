"""Deploy custom_components/monctonwater to the Home Assistant box.

Copies the integration over SMB (the HA OS Samba add-on share) and
verifies every file by SHA-256. Does not restart Home Assistant — run
``python scripts/restart_ha.py`` or restart from the UI afterwards.

Credentials come from environment variables:

    MONCTONWATER_SMB_HOST   (required unless known defaults apply)
    MONCTONWATER_SMB_USER   (default homeassistant)
    MONCTONWATER_SMB_PASS   (required)

Requires: pip install smbprotocol
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import smbclient

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "custom_components" / "monctonwater"

HOST = os.environ.get("MONCTONWATER_SMB_HOST", "")
USER = os.environ.get("MONCTONWATER_SMB_USER", "homeassistant")
PASSWORD = os.environ.get("MONCTONWATER_SMB_PASS")


def main() -> int:
    if not PASSWORD or not HOST:
        print("MONCTONWATER_SMB_HOST and MONCTONWATER_SMB_PASS are required")
        return 2
    dst = rf"\\{HOST}\config\custom_components\monctonwater"

    smbclient.reset_connection_cache()
    smbclient.register_session(
        HOST, username=USER, password=PASSWORD, connection_timeout=15
    )

    smbclient.makedirs(rf"{dst}\translations", exist_ok=True)
    files = sorted(
        p for p in SRC.rglob("*") if p.is_file() and "__pycache__" not in p.parts
    )
    for f in files:
        rel = f.relative_to(SRC)
        target = rf"{dst}\{rel}"
        smbclient.makedirs(
            str(Path(target).parent), exist_ok=True
        )
        with open(f, "rb") as src_fh, smbclient.open_file(target, mode="wb") as dst_fh:
            dst_fh.write(src_fh.read())
        print(f"copied {rel}")

    # Verify by hash.
    ok = True
    for f in files:
        rel = f.relative_to(SRC)
        target = rf"{dst}\{rel}"
        local = hashlib.sha256(f.read_bytes()).hexdigest()
        with smbclient.open_file(target, mode="rb") as fh:
            remote = hashlib.sha256(fh.read()).hexdigest()
        status = "OK" if local == remote else "MISMATCH"
        ok = ok and local == remote
        print(f"{status:8} {rel}")

    print("deploy verified" if ok else "DEPLOY INCOMPLETE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
