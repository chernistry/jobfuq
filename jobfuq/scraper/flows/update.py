# File: update.py
#!/usr/bin/env python3
"""
update.py

This script updates the status of old job listings in the database.
It identifies listings not checked since yesterday with a model_fit_score above a threshold,
rechecks their status (including applicant count) using the scraper.py,
and updates the database accordingly.
"""

import asyncio
import time
import random
import argparse
from jobfuq.database.database import create_connection, load_blacklist
from jobfuq.scraper.core.scraper import LinkedInScraper
from jobfuq.scraper.core.linked_utils import simulate_human_behavior, block_resources
from jobfuq.utils.utils import load_config
from jobfuq.logger.logger import logger, set_verbose
from playwright.async_api import async_playwright

ONE_DAY_MS = 86400000  # One day in milliseconds

async def update_old_job_listings():
    config = load_config("jobfuq/conf/config.toml")
    conn = create_connection(config)
    current_time = int(time.time() * 1000)
    yesterday_threshold = current_time - ONE_DAY_MS

    query = """
        SELECT * FROM job_listings
        WHERE (last_checked IS NULL OR last_checked < ?)
          AND listed_at < ?
          AND model_fit_score > ?
    """
    model_fit_min = 50
    cursor = conn.execute(query, (yesterday_threshold, yesterday_threshold, model_fit_min))
    rows = cursor.fetchall()

    if not rows:
        logger.info("No job listings found that meet the criteria.")
        conn.close()
        return

    col_names = [desc[0] for desc in cursor.description]
    jobs = [dict(zip(col_names, row)) for row in rows]
    logger.info(f"Found {len(jobs)} job listing(s) to update.")

    async with async_playwright() as p:
        headless = config.get("headless", True)
        browser = await p.chromium.launch(headless=headless, slow_mo=50)
        user_agents = config.get("user_agents", ["Mozilla/5.0"])
        context = await browser.new_context(user_agent=random.choice(user_agents))
        await context.route("**/*", block_resources)
        page = await context.new_page()
        await simulate_human_behavior(page)

        time_filter = config.get("time_filter", "2419200")
        blacklist = load_blacklist(conn)
        scraper = LinkedInScraper(config, time_filter, blacklist, playwright=p)

        for job in jobs:
            job_url = job.get("job_url")
            if not job_url:
                logger.warning("Skipping a job with no job_url.")
                continue

            logger.info(f"Rechecking status for job: {job_url}")
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
    set_verbose(args.verbose)
    asyncio.run(update_old_job_listings())

if __name__ == "__main__":
    main()