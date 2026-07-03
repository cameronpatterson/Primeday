#!/usr/bin/env python3
"""
Bullet Ant - Prime Day PC Hardware Bargain Finder
====================================================

Searches Amazon US and Amazon Australia for deals on PC hardware
(3.5" and 2.5" drives, NVMe SSDs, RAM, graphics cards) and flags
items discounted by more than a given threshold, emailing you a
daily summary.

WHY THIS USES THE OFFICIAL PRODUCT ADVERTISING API (PA-API 5.0)
-----------------------------------------------------------------
Amazon actively blocks and legally prohibits scraping of its search
result pages (see their Conditions of Use / robots.txt). Scrapers
get IP-banned or CAPTCHA-walled almost immediately, and the HTML
structure changes constantly, so a raw scraper breaks within days.
The supported, reliable way to search Amazon programmatically is
the Product Advertising API, which is free to use once you're
approved as an Amazon Associate (affiliate) in each marketplace.

SETUP
-----
1. Sign up as an Amazon Associate:
   - US:  https://affiliate-program.amazon.com/
   - AU:  https://affiliate-program.amazon.com.au/
2. Once approved, generate PA-API credentials from the Associates
   Central "Tools -> Product Advertising API" page for each region.
3. pip install python-amazon-paapi
4. Fill in the CREDENTIALS dict below (or set the environment
   variables it falls back to) and run the script.

Note: PA-API requires you to have driven a small number of qualifying
sales within the last 180 days to keep API access active - Amazon's
rule, not this script's. If your account is brand new, there may be
a short grace period; check current terms on the Associates site.

USAGE
-----
    python bullet_ant.py --discount 20 --marketplace both --email

SEASONAL BEHAVIOUR
-------------------
Bullet Ant only actively searches and emails during the week before and
the week after each marketplace's Prime Day event. US and AU dates don't
always match - for 2026, US Prime Day is June 23-26 and AU Prime Day is
July 7-13 - so each region has its own window, computed from
PRIME_DAY_CORE_DATES near the top of the script. Outside those windows,
running the script does nothing and sends no email at all - it just
prints a note and exits. Update PRIME_DAY_CORE_DATES each year once
Amazon announces the new dates.


This prints a table of matching deals to the console, saves them to
bargains_<timestamp>.csv, and (with --email) sends a summary to the
address in BARGAIN_EMAIL_TO.

EMAIL SETUP
-----------
Set these environment variables (or edit EMAIL_CONFIG directly):
    BARGAIN_EMAIL_FROM      the sending email address
    BARGAIN_EMAIL_PASSWORD  an app password for that account (NOT your
                             normal login password)
    BARGAIN_EMAIL_TO        where the daily summary should land
    BARGAIN_SMTP_HOST       defaults to smtp.gmail.com
    BARGAIN_SMTP_PORT       defaults to 587

For Gmail, generate an app password at
https://myaccount.google.com/apppasswords (requires 2-Step Verification
turned on). Other providers (Outlook, iCloud, your own mail server) work
the same way - just change BARGAIN_SMTP_HOST/PORT.

RUNNING IT EVERY 4 HOURS
------------------------
This script only runs once per invocation - it doesn't loop or wait on
its own. Schedule it with cron (Linux/home lab) or Task Scheduler
(Windows). --email now always sends something: a deals summary if it
found any, or a "no update" email with an empty body if it didn't.

    # crontab -e, run every 4 hours on the hour:
    0 */4 * * * /usr/bin/python3 /path/to/bullet_ant.py --email >> /var/log/bullet_ant.log 2>&1

If you're running this on your Debian home lab box, cron is the natural
fit given your existing pfSense/Docker setup.
"""

import argparse
import csv
import datetime as dt
import os
import smtplib
import sys
import time
from dataclasses import dataclass, asdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Iterable

try:
    from amazon_paapi import AmazonApi
except ImportError:
    print(
        "Missing dependency. Install it first with:\n"
        "    pip install python-amazon-paapi\n",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Credentials - fill these in, or export them as environment variables with
# the same names before running the script.
# ---------------------------------------------------------------------------
CREDENTIALS = {
    "US": {
        "key": os.environ.get("AMAZON_US_ACCESS_KEY", ""),
        "secret": os.environ.get("AMAZON_US_SECRET_KEY", ""),
        "tag": os.environ.get("AMAZON_US_PARTNER_TAG", ""),
        "country": "US",
    },
    "AU": {
        "key": os.environ.get("AMAZON_AU_ACCESS_KEY", ""),
        "secret": os.environ.get("AMAZON_AU_SECRET_KEY", ""),
        "tag": os.environ.get("AMAZON_AU_PARTNER_TAG", ""),
        "country": "AU",
    },
}

# ---------------------------------------------------------------------------
# Prime Day windows - the script only searches/emails during the week
# before and the week after each marketplace's Prime Day event. US and AU
# Prime Day dates don't always line up (2026 is a good example: US ran
# June 23-26, AU runs July 7-13), so each region has its own window.
#
# UPDATE THESE EVERY YEAR once Amazon announces the new dates.
# ---------------------------------------------------------------------------
PRIME_DAY_CORE_DATES = {
    "US": (dt.date(2026, 6, 23), dt.date(2026, 6, 26)),
    "AU": (dt.date(2026, 7, 7), dt.date(2026, 7, 13)),
}
WINDOW_PADDING_DAYS = 7  # one week either side of the core event


def active_window(region: str) -> tuple[dt.date, dt.date]:
    start, end = PRIME_DAY_CORE_DATES[region]
    return (
        start - dt.timedelta(days=WINDOW_PADDING_DAYS),
        end + dt.timedelta(days=WINDOW_PADDING_DAYS),
    )


def is_in_window(region: str, today: dt.date | None = None) -> bool:
    today = today or dt.date.today()
    win_start, win_end = active_window(region)
    return win_start <= today <= win_end


# Search terms covering the PC hardware categories you mentioned.
SEARCH_TERMS = [
    "3.5 inch internal hard drive",
    "2.5 inch SSD SATA",
    "NVMe SSD 1TB",
    "NVMe SSD 2TB",
    "DDR5 RAM",
    "DDR4 RAM",
    "graphics card RTX",
    "graphics card RX",
]

# ---------------------------------------------------------------------------
# Email settings - fill these in, or export as environment variables with
# the same names. Works with Gmail (use an App Password, not your normal
# password: https://myaccount.google.com/apppasswords) or any SMTP provider.
# ---------------------------------------------------------------------------
EMAIL_CONFIG = {
    "smtp_host": os.environ.get("BARGAIN_SMTP_HOST", "smtp.gmail.com"),
    "smtp_port": int(os.environ.get("BARGAIN_SMTP_PORT", "587")),
    "sender": os.environ.get("BARGAIN_EMAIL_FROM", ""),
    "password": os.environ.get("BARGAIN_EMAIL_PASSWORD", ""),
    "recipient": os.environ.get("BARGAIN_EMAIL_TO", "cameron@barroni.com"),
}

ITEMS_PER_SEARCH = 10  # PA-API max per page is 10


@dataclass
class Deal:
    marketplace: str
    search_term: str
    title: str
    asin: str
    price: float
    list_price: float
    discount_pct: float
    url: str


def build_client(region: str) -> AmazonApi:
    creds = CREDENTIALS[region]
    if not all([creds["key"], creds["secret"], creds["tag"]]):
        raise RuntimeError(
            f"Missing PA-API credentials for {region}. Set "
            f"AMAZON_{region}_ACCESS_KEY / _SECRET_KEY / _PARTNER_TAG."
        )
    return AmazonApi(creds["key"], creds["secret"], creds["tag"], creds["country"])


def search_deals(client: AmazonApi, region: str, min_discount: float) -> Iterable[Deal]:
    for term in SEARCH_TERMS:
        try:
            results = client.search_items(keywords=term, item_count=ITEMS_PER_SEARCH)
        except Exception as exc:  # PA-API throttles hard - keep going on errors
            print(f"  [{region}] search failed for '{term}': {exc}", file=sys.stderr)
            continue

        items = getattr(results, "items", None) or []
        for item in items:
            try:
                price = item.offers.listings[0].price.amount
                list_price = (
                    item.offers.listings[0].saving_basis.amount
                    if item.offers.listings[0].saving_basis
                    else price
                )
            except (AttributeError, IndexError, TypeError):
                continue

            if not list_price or list_price <= 0:
                continue

            discount_pct = round((1 - price / list_price) * 100, 1)
            if discount_pct >= min_discount:
                yield Deal(
                    marketplace=region,
                    search_term=term,
                    title=item.item_info.title.display_value,
                    asin=item.asin,
                    price=price,
                    list_price=list_price,
                    discount_pct=discount_pct,
                    url=item.detail_page_url,
                )

        # PA-API rate limit is roughly 1 request/second on new accounts
        time.sleep(1.1)


def run(regions: list[str], min_discount: float) -> list[Deal]:
    all_deals: list[Deal] = []
    for region in regions:
        if not is_in_window(region):
            win_start, win_end = active_window(region)
            print(
                f"Skipping {region} - outside its Prime Day window "
                f"({win_start} to {win_end})."
            )
            continue

        print(f"Searching Amazon {region}...")
        client = build_client(region)
        deals = list(search_deals(client, region, min_discount))
        deals.sort(key=lambda d: d.discount_pct, reverse=True)
        all_deals.extend(deals)
    return all_deals


def print_table(deals: list[Deal]) -> None:
    if not deals:
        print("\nNo deals found at or above the requested discount threshold.")
        return

    print(f"\n{'Market':<6} {'Disc%':>6}  {'Price':>9}  {'Was':>9}  Title")
    print("-" * 100)
    for d in deals:
        print(
            f"{d.marketplace:<6} {d.discount_pct:>5.1f}%  "
            f"{d.price:>9.2f}  {d.list_price:>9.2f}  {d.title[:60]}"
        )


def save_csv(deals: list[Deal]) -> str:
    filename = f"bargains_{dt.datetime.now():%Y%m%d_%H%M%S}.csv"
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(deals[0]).keys()) if deals else [])
        writer.writeheader()
        for d in deals:
            writer.writerow(asdict(d))
    return filename


def build_email_body(deals: list[Deal]) -> str:
    if not deals:
        return "No deals met your discount threshold today."

    lines = [f"Found {len(deals)} deal(s) today:\n"]
    for d in deals:
        lines.append(
            f"[{d.marketplace}] {d.discount_pct:.0f}% off - "
            f"{d.title[:80]}\n"
            f"    Now: {d.price:.2f}  Was: {d.list_price:.2f}\n"
            f"    {d.url}\n"
        )
    return "\n".join(lines)


def send_email(deals: list[Deal]) -> None:
    cfg = EMAIL_CONFIG
    if not all([cfg["sender"], cfg["password"], cfg["recipient"]]):
        print(
            "Email not sent - missing BARGAIN_EMAIL_FROM / "
            "BARGAIN_EMAIL_PASSWORD / BARGAIN_EMAIL_TO.",
            file=sys.stderr,
        )
        return

    msg = MIMEMultipart()
    msg["From"] = cfg["sender"]
    msg["To"] = cfg["recipient"]

    if deals:
        msg["Subject"] = f"Bullet Ant - {dt.datetime.now():%d %b %Y %H:%M} ({len(deals)} bargains found)"
        msg.attach(MIMEText(build_email_body(deals), "plain"))
    else:
        msg["Subject"] = f"Bullet Ant - no update ({dt.datetime.now():%d %b %Y %H:%M})"
        msg.attach(MIMEText("", "plain"))

    try:
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
            server.starttls()
            server.login(cfg["sender"], cfg["password"])
            server.send_message(msg)
        print(f"Email sent to {cfg['recipient']}.")
    except Exception as exc:
        print(f"Failed to send email: {exc}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Find Prime Day PC hardware bargains on Amazon.")
    parser.add_argument(
        "--discount", type=float, default=20.0, help="Minimum discount percent to report (default 20)"
    )
    parser.add_argument(
        "--marketplace",
        choices=["us", "au", "both"],
        default="both",
        help="Which marketplace(s) to search",
    )
    parser.add_argument(
        "--email",
        action="store_true",
        help="Email the results (always run daily via cron/Task Scheduler with this flag)",
    )
    args = parser.parse_args()

    regions = {"us": ["US"], "au": ["AU"], "both": ["US", "AU"]}[args.marketplace]

    regions_in_window = [r for r in regions if is_in_window(r)]
    if not regions_in_window:
        print("No requested marketplace is within a Prime Day window right now. Exiting without emailing.")
        return

    deals = run(regions, args.discount)
    print_table(deals)

    if deals:
        path = save_csv(deals)
        print(f"\nSaved {len(deals)} deals to {path}")

    if args.email:
        send_email(deals)


if __name__ == "__main__":
    main()
