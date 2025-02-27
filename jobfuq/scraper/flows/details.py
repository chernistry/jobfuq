import asyncio
import random
import re
from playwright.async_api import async_playwright
from jobfuq.logger.logger import logger, set_verbose
from jobfuq.utils.utils import load_config
from jobfuq.database.database import create_connection, create_table
from jobfuq.scraper.core.scraper import LinkedInScraper
from jobfuq.scraper.core.linked_utils import block_resources, ensure_logged_in, simulate_human_behavior, wait_for_feed

def get_incomplete_jobs(conn):
    """
    Retrieve jobs that are missing key details (e.g. empty description or NULL applicants_count).
    This query assumes that the search flow has already inserted jobs with title and company_url.
    """
    # Adjust the query as needed; here we target jobs with "No Company" and empty description.
    query = (
        "SELECT * FROM main.job_listings "
        "WHERE (trim(description) = '' OR applicants_count IS NULL) "
        "AND (company = 'No Company' OR company IS NULL)"
    )
    cursor = conn.execute(query)
    jobs = cursor.fetchall()
    cols = [desc[0] for desc in cursor.description]
    return [dict(zip(cols, row)) for row in jobs]

async def main(config):
    set_verbose(config.get('verbose', False))
    conn = create_connection(config)
    create_table(conn)

    incomplete_jobs = get_incomplete_jobs(conn)
    if not incomplete_jobs:
        logger.info("No incomplete jobs found.")
        conn.close()
        return

    async with async_playwright() as p:
        headless = config.get("headless", False)
        browser = await p.chromium.launch(headless=headless, slow_mo=50)
        context = await browser.new_context(
            viewport={
                'width': 1280 + random.randint(-50, 50),
                'height': 720 + random.randint(-30, 30)
            },
            user_agent=random.choice(config.get('user_agents', ['Mozilla/5.0']))
        )
        await context.route("**/*", block_resources)
        page = await context.new_page()

        # Perform login using ensure_logged_in.
        creds_pool = config.get('linkedin_credentials', {}).values()
        if not creds_pool:
            logger.error("No LinkedIn credentials provided in config!")
            await browser.close()
            conn.close()
            return
        creds = random.choice(list(creds_pool))
        username, password = creds['username'], creds['password']
        logger.info(f"Logging in with: {username}")
        logged_in_page = await ensure_logged_in(page, username, password, p, config)
        if not logged_in_page:
            logger.error("Login failed. Aborting details flow.")
            await browser.close()
            conn.close()
            return
        page = logged_in_page

        time_filter = config.get("time_filter", "r604800")
        scraper = LinkedInScraper(config, time_filter, {}, playwright=p)

        for job in get_incomplete_jobs(conn):
            job_url = job.get("job_url")
            if not job_url:
                continue
            match = re.search(r'/jobs/view/(\d+)/', job_url)
            if not match:
                logger.error(f"Cannot extract job_id from {job_url}")
                continue
            job_id = match.group(1)
            logger.info(f"Extracting details for job: {job_url}")

            detail_page = await context.new_page()
            await detail_page.route("**/*", block_resources)
            # Optionally, wait for feed if necessary:
            # detail_page = await wait_for_feed(detail_page, p, config)
            job_data = await scraper.get_job_details(detail_page, job_id, conn)
            await detail_page.close()
            if job_data:
                # Merge extracted details with existing minimal data.
                # Preserve title and company_url from the search flow if already set.
                job_data["title"] = job_data["title"] or job.get("title", "")
                job_data["company_url"] = job_data.get("company_url", "") or job.get("company_url", "")
                update_query = (
                    "UPDATE job_listings SET title = ?, company = ?, company_url = ?, location = ?, "
                    "description = ?, remote_allowed = ?, job_state = ?, company_size = ?, company_size_score = ?, "
                    "date = ?, listed_at = ?, applicants_count = ?, overall_relevance = ?, is_posted = ?, "
                    "application_status = ? WHERE job_url = ?"
                )
                params = (
                    job_data["title"],
                    job_data["company"],
                    job_data["company_url"],
                    job_data["location"],
                    job_data["description"],
                    job_data["remote_allowed"],
                    job_data["job_state"],
                    job_data["company_size"],
                    job_data["company_size_score"],
                    job_data["date"],
                    job_data["listed_at"],
                    job_data["applicants_count"],
                    job_data["overall_relevance"],
                    job_data["is_posted"],
                    job_data["application_status"],
                    job_url
                )
                conn.execute(update_query, params)
                conn.commit()
                logger.info(f"Updated job details for: {job_data['title']} @ {job_data['company']}")
        await browser.close()
    conn.close()

if __name__ == "__main__":
    config = load_config('jobfuq/conf/config.toml')
    asyncio.run(main(config))