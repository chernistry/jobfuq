#!/usr/bin/env python3
"""
update_job_status.py

This script implements the following pipeline:
  • Identify the oldest job listings in the database (listed before yesterday)
    that were last checked before yesterday and have a model_fit_score > 50.
  • Recheck their status using the scraper.
  • If the job is still open, update its applicant count.
  • If the job is closed, mark it as closed in the database.
"""

import asyncio
import time
import random
import sqlite3
import argparse
from datetime import datetime
from jobfuq.database import create_connection, load_blacklist
from jobfuq.scraper import LinkedInScraper
from jobfuq.linked_utils import simulate_human_behavior, block_resources
from jobfuq.utils import load_config
from jobfuq.logger import logger, set_verbose
from playwright.async_api import async_playwright

# Constant for one day in milliseconds.
ONE_DAY_MS = 86400000

async def update_old_job_listings():
    # Load configuration and connect to the database.
    config = load_config("jobfuq/conf/config.toml")
    conn = create_connection(config)
    current_time = int(time.time() * 1000)
    print(current_time)

    # Use yesterday's timestamp for both thresholds.
    yesterday_threshold = current_time - ONE_DAY_MS
    last_checked_threshold = yesterday_threshold
    listed_threshold = yesterday_threshold
    model_fit_min = 50

    # SQL query: select listings where last_checked is NULL or older than yesterday,
    # listed before yesterday, and model_fit_score is above 50.
    query = """
        SELECT * FROM job_listings
        WHERE (last_checked IS NULL OR last_checked < ?)
          AND listed_at < ?
          AND model_fit_score > ?
    """
    cursor = conn.execute(query, (last_checked_threshold, listed_threshold, model_fit_min))
    rows = cursor.fetchall()

    if not rows:
        logger.info("No job listings found that meet the criteria.")
        conn.close()
        return

    # Convert rows to dictionaries using the cursor description.
    col_names = [desc[0] for desc in cursor.description]
    jobs = [dict(zip(col_names, row)) for row in rows]
    logger.info(f"Found {len(jobs)} job listing(s) to update.")

    # Use Playwright to launch a browser session.
    async with async_playwright() as p:
        headless = config.get("headless", True)
        browser = await p.chromium.launch(headless=headless, slow_mo=50)

        # Create a browser context and a page. Use a random user agent.
        user_agents = config.get("user_agents", ["Mozilla/5.0"])
        context = await browser.new_context(
            user_agent=random.choice(user_agents)
        )
        # Optionally block unneeded resources.
        await context.route("**/*", block_resources)
        page = await context.new_page()
        await simulate_human_behavior(page)

        # Initialize the scraper.
        time_filter = config.get("time_filter", "2419200")
        blacklist = load_blacklist(conn)
        scraper = LinkedInScraper(config, time_filter, blacklist, playwright=p)

        # Process each job listing.
        for job in jobs:
            job_url = job.get("job_url")
            if not job_url:
                logger.warning("Skipping a job with no job_url.")
                continue

            logger.info(f"Rechecking status for job: {job_url}")
            # The update_existing_job method handles both open and closed cases:
            # if the job is closed, it marks it as CLOSED; if open, it updates the applicant count.
            updated = await scraper.update_existing_job(conn, job_url, page)
            if updated:
                state = updated.get("job_state", "UNKNOWN")
                logger.info(f"Job {job_url} updated: state={state}")
            else:
                logger.error(f"Failed to update job: {job_url}")

        await browser.close()
    conn.close()

def main():
    parser = argparse.ArgumentParser(
        description="Update old job listings status with increased verbosity option (-v)."
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Increase output verbosity")
    args = parser.parse_args()

    # Set logger verbosity based on the provided flag.
    set_verbose(args.verbose)
    asyncio.run(update_old_job_listings())

if __name__ == "__main__":
    main()
