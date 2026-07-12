"""Wipe collected monitoring history (probe results, uptime, incidents,
shard snapshots, alert state) for a fresh start. Announcements, admin
accounts, webhook subscribers, and meta_kv are kept.

Lives inside the package (not scripts/) so it exists in the Docker image:

    docker exec maid-status python -m status_service.reset_monitoring --yes

The same operation is available to owners in the /admin panel.
"""

from __future__ import annotations

import sys

from . import db


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if "--yes" not in args:
        print("This wipes ALL monitoring history (announcements/accounts/subscribers are kept).")
        print("Re-run with --yes to confirm.")
        return 1
    counts = db.reset_monitoring_data()
    for table, n in counts.items():
        print(f"{table}: {n} rows deleted")
    print("Done — fresh data starts on the next probe cycle.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
