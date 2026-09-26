"""
Start the dashboard (web UI + the company's work loop).

First time:   python run_dashboard.py --set-password
Then:         python run_dashboard.py
Open:         http://localhost:8765  (or http://<this-PC-IP>:8765 from the laptop)
"""

import argparse
import getpass
import socket
import sys
from pathlib import Path

import uvicorn

from core.company import Company
from dashboard.auth import MIN_PASSWORD_LENGTH, AuthStore
from dashboard.config_admin import ConfigAdmin
from dashboard.server import create_app


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the LAN interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> int:
    if sys.stdout is None or sys.stderr is None:   # started hidden with pythonw (autostart)
        Path("logs").mkdir(exist_ok=True)
        sys.stdout = sys.stderr = open("logs/dashboard.out", "a", encoding="utf-8", buffering=1)
    ap = argparse.ArgumentParser(description="AI Company dashboard")
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = reachable on the LAN; "
                                                      "127.0.0.1 = this PC only")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", default="data/company.db")
    ap.add_argument("--workspace", default=None,
                    help="folder for produced files (default: workspace_dir in config/company.json)")
    ap.add_argument("--auth-file", default="data/dashboard_auth.json")
    ap.add_argument("--set-password", action="store_true", help="set the dashboard password")
    ap.add_argument("--local-preview-no-auth", action="store_true",
                    help="skip login; only allowed with --host 127.0.0.1 (for demo previews)")
    args = ap.parse_args()

    if args.local_preview_no_auth:
        if args.host != "127.0.0.1":
            print("--local-preview-no-auth is only allowed with --host 127.0.0.1")
            return 1
        print("WARNING: login disabled (local preview). Only this PC can connect.")
        app = create_app(lambda: Company(args.db, workspace_dir=args.workspace),
                         AuthStore(args.auth_file), ConfigAdmin(), local_preview_no_auth=True)
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
        return 0

    auth = AuthStore(args.auth_file)
    if args.set_password:
        pw = getpass.getpass(f"New dashboard password (min {MIN_PASSWORD_LENGTH} chars): ")
        if pw != getpass.getpass("Repeat password: "):
            print("Passwords do not match.")
            return 1
        auth.set_password(pw)
        print(f"Password saved (hashed) to {args.auth_file}")
        return 0
    if not auth.is_configured():
        print("No dashboard password set yet. Run:  python run_dashboard.py --set-password")
        return 1

    app = create_app(lambda: Company(args.db, workspace_dir=args.workspace), auth, ConfigAdmin())
    print(f"Dashboard:  http://localhost:{args.port}")
    if args.host == "0.0.0.0":
        print(f"From LAN:   http://{lan_ip()}:{args.port}")
    print("Press Ctrl+C to stop.")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
