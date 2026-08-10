import logging
from datetime import datetime
from pathlib import Path

log_dir = Path(__file__).resolve().parent.parent / "logs"
log_dir.mkdir(exist_ok=True)

logging.basicConfig(
    filename=log_dir / "scraper.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

def main():
    logging.info("=== Run started (STUB — no real scraping logic yet) ===")
    logging.info("This is a placeholder to validate cron + logging mechanics.")
    logging.info("=== Run finished ===")
    print(f"Stub run completed at {datetime.now()}")

if __name__ == "__main__":
    main()
