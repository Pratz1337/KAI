"""
Driver management utility.

Installs, starts, stops, and removes the AikKmdfIoctl kernel driver
using the Windows Service Control Manager (sc.exe).

Must be run as Administrator.

Usage:
    python tools/driver_loader.py install  --sys path\\to\\AikKmdfIoctl.sys
    python tools/driver_loader.py start
    python tools/driver_loader.py stop
    python tools/driver_loader.py remove
    python tools/driver_loader.py status
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

SERVICE_NAME = "AikKmdfIoctl"


def _run(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = (r.stdout + "\n" + r.stderr).strip()
    return r.returncode, out


def install(sys_path: str) -> None:
    abspath = os.path.abspath(sys_path)
    if not os.path.isfile(abspath):
        print(f"ERROR: driver file not found: {abspath}", file=sys.stderr)
        sys.exit(1)

    # Ensure test signing is on – required for unsigned drivers.
    rc, out = _run(["bcdedit", "/enum", "{current}"])
    if "testsigning" not in out.lower() or "yes" not in out.lower():
        print("WARNING: Test Signing does not appear to be enabled.")
        print("  Run:  bcdedit /set testsigning on")
        print("  Then reboot before loading unsigned drivers.\n")

    rc, out = _run([
        "sc", "create", SERVICE_NAME,
        "type=", "kernel",
        "start=", "demand",
        f"binPath=", abspath,
    ])
    print(f"sc create -> {out}")
    if rc != 0 and "already exists" not in out.lower():
        sys.exit(rc)


def start() -> None:
    rc, out = _run(["sc", "start", SERVICE_NAME])
    print(f"sc start -> {out}")


def stop() -> None:
    rc, out = _run(["sc", "stop", SERVICE_NAME])
    print(f"sc stop -> {out}")


def remove() -> None:
    stop()
    rc, out = _run(["sc", "delete", SERVICE_NAME])
    print(f"sc delete -> {out}")


def status() -> None:
    rc, out = _run(["sc", "query", SERVICE_NAME])
    print(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="AIK kernel driver manager")
    sub = ap.add_subparsers(dest="action", required=True)

    p_install = sub.add_parser("install", help="Register the driver with SCM")
    p_install.add_argument("--sys", required=True, help="Path to AikKmdfIoctl.sys")

    sub.add_parser("start", help="Start the driver service")
    sub.add_parser("stop", help="Stop the driver service")
    sub.add_parser("remove", help="Stop + delete the driver service")
    sub.add_parser("status", help="Query driver service status")

    args = ap.parse_args()

    if args.action == "install":
        install(args.sys)
    elif args.action == "start":
        start()
    elif args.action == "stop":
        stop()
    elif args.action == "remove":
        remove()
    elif args.action == "status":
        status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
