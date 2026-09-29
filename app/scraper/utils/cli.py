"""Shared `python -m` runner for the site scrapers."""
import argparse
import json


def run_cli(scraper_cls, default_slug):
    parser = argparse.ArgumentParser(description=scraper_cls.__name__)
    parser.add_argument("--slug", default=default_slug, help="page slug (default: %(default)s)")
    parser.add_argument("--date", help="release date, DD-MM-YYYY; omit to print the whole page")
    args = parser.parse_args()
    with scraper_cls() as scraper:
        result = scraper.fetch_release(args.slug, args.date) if args.date else scraper.fetch_page(args.slug)
    print(json.dumps(result, indent=2, default=str))
