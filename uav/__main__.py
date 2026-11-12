"""Run one application process; multiple web workers would duplicate hardware readers."""
import argparse
import logging
import os
import uvicorn
from .config import ROOT, load_settings
from .web import create_app


def main():
    parser = argparse.ArgumentParser(description="UAV V2 ground dashboard")
    parser.add_argument("--demo", action="store_true", help="Synthetic preview; separate database, port 8081")
    parser.add_argument("--config", default=str(ROOT / "config/settings.json"))
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_settings(args.config, demo=args.demo)
    if not (ROOT / "config/users.json").is_file():
        parser.error("Missing config/users.json; run scripts/manage.py user USERNAME")
    # Proxy headers stay disabled: the public HTTPS origin is explicitly configured.
    uvicorn.run(create_app(config), host=config["web"]["host"], port=config["web"]["port"],
                workers=1, proxy_headers=False, access_log=False, timeout_graceful_shutdown=10, limit_concurrency=64)


if __name__ == "__main__":
    main()
