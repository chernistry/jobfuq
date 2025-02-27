import asyncio, random, re, time
from playwright.async_api import async_playwright
from jobfuq.logger.logger import logger, set_verbose
from jobfuq.utils.utils import load_config
from jobfuq.database.database import (
    create_connection, create_table, create_blacklist_table,
    create_blacklisted_companies_table, load_blacklist, job_exists, insert_job_minimal
)
from jobfuq.scraper.core.scraper import LinkedInScraper
from jobfuq.scraper.core.linked_utils import block_resources, ensure_logged_in, simulate_human_behavior

async def main(config):
    set_verbose(config.get('verbose', False))
    conn = create_connection(config)
    create_table(conn)
    create_blacklist_table(conn)
    create_blacklisted_companies_table(conn)
    try:
        blacklist = load_blacklist(conn)
    except Exception as e:
        logger.error(f"Error loading blacklist: {e}")
        blacklist = {'blacklist': set(), 'whitelist': set()}

    async with async_playwright() as p:
        headless = config.get("headless", False)
        browser = await p.chromium.launch(headless=headless, slow_mo=50)
        context = await browser.new_context(
            viewport={'width': 1280 + random.randint(-50, 50), 'height': 720 + random.randint(-30, 30)},
            user_agent=random.choice(config.get('user_agents', ['Mozilla/5.0']))
        )
        await context.route("**/*", block_resources)
        page = await context.new_page()

        # Perform login explicitly using ensure_logged_in.
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
            logger.error("Login failed. Aborting search flow.")
            await browser.close()
            conn.close()
            return
        page = logged_in_page  # Use the page returned after successful login

        time_filter = config.get("time_filter", "r604800")
        scraper = LinkedInScraper(config, time_filter, blacklist, playwright=p)
        search_queries = config.get("search_queries", [{"keywords": "DevOps", "location": "Remote", "remote": None}])
        for query in search_queries:
            keywords = query.get("keywords")
            location = query.get("location")
            remote = query.get("remote")
            logger.info(f"Searching jobs with keywords={keywords}, location={location}, remote={remote}")
            # Gather job cards from the search results page.
            job_infos = await scraper.search_jobs(page, keywords, location, remote)
            logger.info(f"Found {len(job_infos)} job listings")
            for info in job_infos:
                job_url = f"{scraper.base_url}/jobs/view/{info['job_id']}/"
                if job_exists(conn, job_url):
                    logger.debug(f"Job {job_url} already exists; skipping.")
                    continue
                # Ensure that company_url key is present.
                if "company_url" not in info:
                    info["company_url"] = ""
                # If search results already provide title and company, use them.
                # Additional details will be updated later via the details flow.
                info["job_url"] = job_url
                if "title" not in info or not info["title"]:
                    info["title"] = "No Title"
                if "company" not in info or not info["company"]:
                    info["company"] = "No Company"
                insert_job_minimal(conn, info)
                conn.commit()
                logger.info(f"Inserted job from search: {info['title']} @ {info['company']}")
        await browser.close()
    conn.close()

if __name__ == "__main__":
    from jobfuq.utils.utils import load_config
    config = load_config('jobfuq/conf/config.toml')
    asyncio.run(main(config))