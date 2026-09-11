from __future__ import annotations

import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from trader import clients, load_config, log_event, scan_once


def main() -> None:
    load_dotenv()
    cfg = load_config()
    if not bool(cfg.get("paper_only", True)):
        raise RuntimeError("paper_only must remain true in this build.")
    api = clients()
    print("Paper-trading bot started. Press Ctrl+C to stop.", flush=True)
    while True:
        try:
            for message in scan_once(cfg, api):
                print(f"{datetime.now(timezone.utc).isoformat()} {message}", flush=True)
        except KeyboardInterrupt:
            print("Bot stopped.", flush=True)
            return
        except Exception as exc:
            log_event("SYSTEM", "scan_error", details=repr(exc))
            print(f"Scan error: {exc}", flush=True)
        time.sleep(max(30, int(cfg["scan_interval_seconds"])))


if __name__ == "__main__":
    main()

