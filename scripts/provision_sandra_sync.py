#!/usr/bin/env python3
"""Generate Sandra's private sync token and place it on the browser clipboard."""

from __future__ import annotations

import os
import secrets
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KK_ENV = ROOT / "profiles" / "kk" / "private" / ".env"
SANDRA_ENV = ROOT / "profiles" / "sandra" / "private" / ".env"


def read_env(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def main() -> int:
    kk = read_env(KK_ENV)
    sandra = read_env(SANDRA_ENV)
    token = sandra.get("COVER_LETTER_SYNC_TOKEN") or secrets.token_urlsafe(48)
    sandra["COVER_LETTER_SYNC_URL"] = kk["COVER_LETTER_SYNC_URL"]
    sandra["COVER_LETTER_SYNC_TOKEN"] = token
    existing = [line for line in SANDRA_ENV.read_text(encoding="utf-8-sig").splitlines()
                if not line.startswith("COVER_LETTER_SYNC_URL=") and not line.startswith("COVER_LETTER_SYNC_TOKEN=")]
    existing.extend([
        f"COVER_LETTER_SYNC_URL={sandra['COVER_LETTER_SYNC_URL']}",
        f"COVER_LETTER_SYNC_TOKEN={token}",
    ])
    SANDRA_ENV.write_text("\n".join(existing) + "\n", encoding="utf-8")
    if os.name == "nt":
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(token)
        root.update()
        root.destroy()
    print("Sandra sync token provisioned locally and copied to the Windows clipboard.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
