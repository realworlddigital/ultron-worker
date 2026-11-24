"""David's one-time Google sign-in for the Drive feeder (the feeder never runs this).

    python -m ultron_worker.feeds.gdrive_auth --client-secrets client_secret.json --out PATH

Runs Google's standard installed-app flow in the browser on this machine (scope
``drive.readonly``) and writes the authorized-user JSON that ``media_feeders.gdrive.auth_file``
points at. Needs the ``gdrive-auth`` extra (google-auth-oauthlib).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .gdrive import SCOPE


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ultron_worker.feeds.gdrive_auth")
    ap.add_argument("--client-secrets", required=True, help="the OAuth desktop client JSON")
    ap.add_argument("--out", required=True, help="where to write the sign-in (auth_file)")
    args = ap.parse_args(argv)
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("needs google-auth-oauthlib: pip install 'ultron-worker[gdrive-auth]'")
        return 2
    flow = InstalledAppFlow.from_client_secrets_file(args.client_secrets, [SCOPE])
    creds = flow.run_local_server(port=0, open_browser=True)
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    print(f"signed in; wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
