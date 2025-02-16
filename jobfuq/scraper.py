#!/usr/bin/env python3
"""
LinkedIn Scraper (Enhanced)

Scrapes LinkedIn job listings using Playwright.
Loads main config from config.toml and LinkedIn-specific config from linked_config.toml.
Supports robust selectors, predictive pagination, concurrent detail-page extraction,
resource blocking, dynamic DOM handling, and fallback logic.
Also supports a debug mode to scrape a single job and exit, and an optional processing step.
"""

import asyncio
import argparse
import json
import math
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from jobfuq.logger import logger, set_verbose
from jobfuq.linked_utils import (
    ensure_logged_in, simulate_human_behavior,
    get_company_size, get_company_size_score, block_resources
)
from jobfuq.utils import load_config  # Loads TOML files
from jobfuq.database import (
    create_connection, create_table, create_blacklist_table, load_blacklist,
    insert_job, job_exists, is_company_blacklisted
)
from jobfuq.processor import process_and_rank_jobs
from jobfuq.llm_handler import AIModel

SPEED_QUANTIFIER: int = 1


# ------------------------------------------------------------------------------
# LinkedInScraper Class
# ------------------------------------------------------------------------------
class LinkedInScraper:
    """
    A class to scrape LinkedIn job listings with multiple fallback selectors,
    concurrent detail-page extraction, and blacklist checking.

    All variable configuration (URLs, selectors, timeouts, attributes, etc.)
    is loaded dynamically from an external TOML file (linked_config.toml).
    """

    def __init__(self, config: Dict[str, Any], time_filter: str, blacklist_data: Dict[str, Any]) -> None:
        """
        Initialize the scraper with general configuration, time filter, blacklist data,
        and load all scraper configuration from 'jobfuq/conf/linked_config.toml'.

        :param config: General configuration dictionary.
        :param time_filter: Time filter string (e.g., 'r604800').
        :param blacklist_data: Dictionary with blacklist and whitelist sets.
        """
        self.config: Dict[str, Any] = config
        self.time_filter: str = time_filter
        self.blacklist_data: Dict[str, Any] = blacklist_data
        # Load the "linked" config from the renamed file
        try:
            self.scraper_config: Dict[str, Any] = load_config("jobfuq/conf/linked_config.toml")
            logger.info("Successfully loaded scraper configuration from linked_config.toml")
        except Exception as e:
            logger.error(f"Failed to load scraper configuration: {e}")
            self.scraper_config = {}
        # Base URL is now configurable.
        self.base_url: str = self.scraper_config.get("urls", {}).get("base_url", "https://www.linkedin.com")
        self.wait_until: str = self.scraper_config.get("urls", {}).get("wait_until", "domcontentloaded")
        self.selector_timeout: int = self.scraper_config.get("timeouts", {}).get("selector_timeout", 30000)
        self.text_timeout: int = self.scraper_config.get("timeouts", {}).get("get_text_timeout", 5000)
        self.job_id_attrs: List[str] = self.scraper_config.get("attributes", {}).get("job_id",
                                                                                     ["data-occludable-job-id", "data-job-id", "data-id"]
                                                                                     )
        self.company_size_cache: Dict[str, str] = {}

    async def search_jobs(self, page: Any, keywords: str, location: str, remote: Optional[bool] = None) -> List[Dict[str, Any]]:
        """
        Navigate to the LinkedIn jobs search URL and collect job cards via infinite scroll and pagination.

        :param page: The Playwright page instance.
        :param keywords: Search keywords.
        :param location: Search location.
        :param remote: Optional flag for remote jobs.
        :return: A list of job information dictionaries.
        """
        # Build search URL using pattern from config
        search_pattern = self.scraper_config.get("urls", {}).get("search_url_pattern",
                                                                 "/jobs/search/?keywords={keywords}&location={location}&f_TPR={time_filter}")
        search_url = self.base_url + search_pattern.format(
            keywords=keywords,
            location=location,
            time_filter=self.time_filter
        )
        if remote is True:
            search_url += "&f_WT=2"
        elif remote is False:
            search_url += "&f_WT=1"
        logger.info(f"Navigating to: {search_url}")
        await page.goto(search_url, wait_until=self.wait_until)
        logger.info(f"Landed on: {page.url}")
        await simulate_human_behavior(page)
        logger.info("Waiting for job list container...")

        # Use job list selectors from config
        job_list_selectors = self.scraper_config.get("jobs", {}).get("job_list_selectors", [])
        for sel in job_list_selectors:
            try:
                await page.wait_for_selector(sel, timeout=self.selector_timeout)
                logger.info(f"Job list found with selector => {sel}")
                break
            except PlaywrightTimeoutError:
                logger.debug(f"No job list with {sel}, trying next...")

        job_infos: List[Dict[str, Any]] = []
        page_num: int = 1
        max_postings: int = self.config.get('max_postings', 100)
        while len(job_infos) < max_postings:
            current_url: str = page.url
            logger.info(f"Current page => {current_url}")
            new_infos = await self.extract_job_infos(page)
            job_infos.extend(new_infos)
            # Remove duplicates by job_id
            job_infos = list({x["job_id"]: x for x in job_infos}.values())
            logger.info(f"Total job infos so far => {len(job_infos)}")
            if len(job_infos) >= max_postings:
                break
            old_count: int = len(job_infos)
            # Basic scroll
            await page.evaluate("window.scrollBy(0, 5000)")
            await asyncio.sleep(random.uniform(2, 3) * SPEED_QUANTIFIER)
            new_infos_2 = await self.extract_job_infos(page)
            job_infos.extend(new_infos_2)
            job_infos = list({x["job_id"]: x for x in job_infos}.values())
            if len(job_infos) == old_count:
                logger.info("No new postings after scroll. Attempting pagination.")
                if not await self.go_to_next_page(page, page_num):
                    logger.info("Pagination failed; assuming no more pages.")
                    break
                if page.url == current_url:
                    logger.info("Page URL unchanged after pagination; no more pages available.")
                    break
                page_num += 1

        return job_infos[:max_postings]

    async def extract_job_infos(self, page: Any) -> List[Dict[str, Any]]:
        """
        Extract minimal job info from job cards using selectors loaded externally.

        :param page: The Playwright page instance.
        :return: A list of dictionaries with basic job data.
        """
        results: List[Dict[str, Any]] = []
        logger.info("Extracting job cards from current page...")

        card_selectors = self.scraper_config.get("jobs", {}).get("job_card_selectors", [])
        job_cards: List[Any] = []
        for sel in card_selectors:
            try:
                found = await page.query_selector_all(sel)
                if found:
                    job_cards = found
                    logger.info(f"Found {len(found)} job cards using selector: {sel}")
                    break
            except Exception as ex:
                logger.debug(f"Error finding job cards with {sel}: {ex}")

        if not job_cards:
            logger.debug("No job cards found with provided selectors.")
            return results

        blacklist: set = self.blacklist_data.get("blacklist", set())
        whitelist: set = self.blacklist_data.get("whitelist", set())

        # Load card-level selectors for various fields from config
        title_selectors = self.scraper_config.get("card", {}).get("title_selectors", [])
        company_selectors = self.scraper_config.get("card", {}).get("company_selectors", [])
        location_selectors = self.scraper_config.get("card", {}).get("location_selectors", [])
        snippet_selectors = self.scraper_config.get("card", {}).get("snippet_selectors", [])
        applicants_selectors = self.scraper_config.get("card", {}).get("applicants_selectors", [])
        company_size_selectors = self.scraper_config.get("card", {}).get("company_size_selectors", [])

        for card in job_cards:
            title_elem = None
            for sel in title_selectors:
                title_elem = await card.query_selector(sel)
                if title_elem:
                    break
            title = (await title_elem.text_content()).strip() if title_elem else ""
            logger.info(f"Extracted job title: '{title}'")

            title_lower = title.lower()
            if title and any(bk in title_lower for bk in blacklist) and not any(wk in title_lower for wk in whitelist):
                logger.info(f"Skipped blacklisted title => '{title}'")
                continue

            job_id = await self.extract_job_id(card)
            if not job_id:
                logger.warning(f"Skipping job due to missing job ID: '{title}'")
                continue

            company = ""
            for sel in company_selectors:
                elem = await card.query_selector(sel)
                if elem:
                    company = (await elem.text_content()).strip()
                    if company:
                        break

            loc = ""
            for sel in location_selectors:
                elem = await card.query_selector(sel)
                if elem:
                    loc = (await elem.text_content()).strip()
                    if loc:
                        break

            descr = ""
            for sel in snippet_selectors:
                elem = await card.query_selector(sel)
                if elem:
                    descr = (await elem.text_content()).strip()
                    if descr:
                        break

            applicants_count = None
            for sel in applicants_selectors:
                elem = await card.query_selector(sel)
                if elem:
                    text = (await elem.text_content()) or ""
                    match = re.search(r'(\d+)', text)
                    if match:
                        applicants_count = int(match.group(1))
                        break

            csize = None
            for sel in company_size_selectors:
                elem = await card.query_selector(sel)
                if elem:
                    txt = await elem.text_content()
                    if txt:
                        csize = txt.strip()
                        break

            job_info: Dict[str, Any] = {
                "job_id": job_id,
                "title": title,
                "company": company,
                "location": loc,
                "description": descr,
                "applicants_count": applicants_count,
                "company_size": csize or "Unknown"
            }
            results.append(job_info)

        logger.info(f"Extracted {len(results)} job cards (after blacklist check).")
        return results

    async def extract_job_id(self, card: Any) -> Optional[str]:
        """
        Extract the job ID from a job card element using attribute names from config.

        :param card: The job card element.
        :return: The job ID string if found, otherwise None.
        """
        for attr in self.job_id_attrs:
            val = await card.get_attribute(attr)
            if val:
                return val
        return None

    async def go_to_next_page(self, page: Any, current_page: int) -> bool:
        """
        Navigate to the next page by clicking pagination buttons using selectors from config.

        :param page: The Playwright page instance.
        :param current_page: The current page number.
        :return: True if pagination was successful, otherwise False.
        """
        next_num: int = current_page + 1
        logger.info(f"Attempting to go to next page => Page {next_num}")

        pagination_selectors = self.scraper_config.get("pagination", {}).get("selectors", [])
        for sel in pagination_selectors:
            try:
                formatted_sel = sel.format(page=next_num)
            except Exception:
                formatted_sel = sel
            try:
                btn = await page.query_selector(formatted_sel)
                if btn:
                    disabled = (await btn.get_attribute("disabled")) or (await btn.get_attribute("aria-disabled"))
                    if disabled and disabled.lower() in ["true", "disabled"]:
                        logger.info(f"Pagination button '{formatted_sel}' is disabled.")
                        continue
                    logger.info(f"Clicking pagination => '{formatted_sel}'")
                    await btn.click()
                    await page.wait_for_load_state(self.wait_until)
                    await asyncio.sleep(2)
                    return True
            except Exception as e:
                logger.debug(f"Error clicking pagination '{formatted_sel}': {e}")

        logger.info(f"No valid pagination found for page {next_num}, stopping.")
        return False

    async def get_field_content(self, page: Any, selectors: List[str], default: str = "") -> str:
        """
        Return the text content from the first element found among a list of selectors.

        :param page: The Playwright page instance.
        :param selectors: List of CSS selectors.
        :param default: Default text if none found.
        :return: Extracted text content.
        """
        for sel in selectors:
            content = await self.get_text_content(page, sel, default="")
            if content:
                return content
        return default

    async def get_job_details(self, page: Any, job_id: str, search_card_info: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """
        Load the full job detail page and extract additional information using selectors from config.

        :param page: The Playwright page instance.
        :param job_id: The unique job identifier.
        :param search_card_info: Fallback info from search results.
        :return: Dictionary of job details or None on failure.
        """
        jurl: str = f"{self.base_url}/jobs/view/{job_id}/"
        logger.info(f"Navigating to job detail => {jurl}")

        try:
            await page.goto(jurl, wait_until=self.wait_until)
            await simulate_human_behavior(page)
        except PlaywrightTimeoutError:
            logger.error(f"Timeout loading detail => job {job_id}")
            return None
        except Exception as e:
            logger.error(f"Error loading detail => job {job_id}: {e}")
            return None

        try:
            detail = self.scraper_config.get("detail", {})
            title_selectors = detail.get("title_selectors", [])
            company_selectors = detail.get("company_selectors", [])
            location_selectors = detail.get("location_selectors", [])
            description_selectors = detail.get("description_selectors", [])
            posted_time_selectors = detail.get("posted_time_selectors", [])
            company_url_selectors = detail.get("company_url_selectors", [])
            company_size_selectors = detail.get("company_size_selectors", [])
            applicants_detail_selectors = detail.get("applicants_detail_selectors", [])

            title: str = search_card_info.get("title", "") if search_card_info else ""
            company: str = search_card_info.get("company", "") if search_card_info else ""
            loc: str = search_card_info.get("location", "") if search_card_info else ""
            descr: str = search_card_info.get("description", "") if search_card_info else ""

            if not title:
                title = await self.get_field_content(page, title_selectors, default="")
            if not company:
                company = await self.get_field_content(page, company_selectors, default="")
            if not loc:
                loc = await self.get_field_content(page, location_selectors, default="")
            if not descr:
                descr = await self.get_field_content(page, description_selectors, default="")

            posted_t: str = await self.get_field_content(page, posted_time_selectors, default="")
            date_str: str = self.parse_posting_date(posted_t)
            company_url: str = await self.get_field_content(page, company_url_selectors, default="")
            company_size: str = await self.get_field_content(page, company_size_selectors, default="Unknown")
            applicants_text: str = await self.get_field_content(page, applicants_detail_selectors, default="Unknown")
            if applicants_text != "Unknown":
                match = re.search(r'\d+', applicants_text)
                applicants_count: Optional[int] = int(match.group(0)) if match else None
            else:
                applicants_count = None

            remote_flag: bool = any(r in descr.lower() for r in ["remote", "wfh", "work from home", "work-from-home"])

            job_data: Dict[str, Any] = {
                "job_id": job_id,
                "title": title.strip(),
                "company": company.strip(),
                "company_url": company_url.strip(),
                "location": loc.strip(),
                "description": self.clean_html(descr.strip()),
                "company_size": company_size.strip(),
                "applicants_count": applicants_count,
                "remote_allowed": remote_flag,
                "job_url": jurl,
                "date": date_str,
                "listed_at": int(time.time() * 1000),
                "job_state": "ACTIVE"
            }
            logger.info(f"Extracted job details: {job_data['title']} at {job_data['company']}")
            return job_data

        except Exception as e:
            logger.error(f"Failed to extract job details for job {job_id}: {e}")
            return None

    async def get_text_content(self, page: Any, selector: str, default: str = "") -> str:
        """
        Attempt to extract text content from an element via a given selector.

        :param page: The Playwright page instance.
        :param selector: CSS selector.
        :param default: Default text if extraction fails.
        :return: Extracted text content.
        """
        try:
            el = await page.wait_for_selector(selector, timeout=self.text_timeout)
            if el:
                txt = await el.text_content()
                if txt:
                    return txt.strip()
        except PlaywrightTimeoutError:
            pass
        except Exception as e:
            logger.debug(f"get_text_content error => {e}")
        return default

    def parse_posting_date(self, posted_time: Optional[str]) -> str:
        """
        Parse a posting date string and return a normalized date in 'YYYY-MM-DD' format.

        :param posted_time: Raw posted time string.
        :return: Normalized date string.
        """
        if not posted_time:
            return datetime.now().strftime('%Y-%m-%d')
        pt = posted_time.lower()
        try:
            if "minute" in pt or "hour" in pt or "just now" in pt:
                return datetime.now().strftime('%Y-%m-%d')
            elif "yesterday" in pt:
                return (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
            elif "day" in pt:
                days = int(re.search(r'(\d+)', pt).group())
                return (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            elif "week" in pt:
                weeks = int(re.search(r'(\d+)', pt).group())
                return (datetime.now() - timedelta(weeks=weeks)).strftime('%Y-%m-%d')
        except Exception as e:
            logger.error(f"Error parsing posting date: {e}")
        return datetime.now().strftime('%Y-%m-%d')

    def clean_html(self, txt: str) -> str:
        """
        Remove HTML tags and extra whitespace from a string.

        :param txt: Raw HTML text.
        :return: Cleaned text.
        """
        if not txt:
            return ""
        c = re.sub(r"<[^>]+>", "", txt)
        return re.sub(r"\s+", " ", c).strip()


async def evaluate_job(ai_model: AIModel, job: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluate a job by merging original job data with AI evaluation results.

    :param ai_model: Instance of AIModel.
    :param job: Dictionary containing the job data.
    :return: Combined job data and evaluation metrics.
    """
    evaluation: Dict[str, Any] = await ai_model.evaluate_job_fit(job)
    return {**job, **evaluation}


async def run_scrape(config: Dict[str, Any], browser: Any, search_queries: List[Dict[str, Any]], manual_login: bool, endless: bool = False) -> None:
    """
    Main function to run job scraping. Yields job dictionaries as found.
    In endless mode, loops indefinitely with a pause between rounds.

    :param config: General configuration dictionary.
    :param browser: Playwright browser instance.
    :param search_queries: List of search query dictionaries.
    :param manual_login: Whether to use manual login.
    :param endless: If True, scrape continuously.
    """
    if endless:
        while True:
            async for job in get_jobcards(config, browser, search_queries, manual_login):
                pass
            logger.info("Scraping round complete. Waiting 60 sec before next round...")
            await asyncio.sleep(60)
    else:
        async for job in get_jobcards(config, browser, search_queries, manual_login):
            pass


async def get_jobcards(config: Dict[str, Any], browser: Any, search_queries: List[Dict[str, Any]], manual_login: bool = False) -> Any:
    """
    Create a browser context, log in, and iterate over each search query to extract job data.
    Yields job data as dictionaries.

    :param config: General configuration dictionary.
    :param browser: Playwright browser instance.
    :param search_queries: List of search query dictionaries.
    :param manual_login: Whether to use manual login.
    :return: Yields job data dictionaries.
    """
    conn = create_connection(config)
    create_table(conn)
    create_blacklist_table(conn)
    try:
        blacklist = load_blacklist(conn)
    except Exception as e:
        logger.error(f"Error loading blacklist: {e}")
        blacklist = {"blacklist": set(), "whitelist": set()}

    context = await browser.new_context(
        viewport={"width": 1280 + random.randint(-50, 50), "height": 720 + random.randint(-30, 30)},
        user_agent=random.choice(config.get('user_agents', ["Mozilla/5.0"]))
    )
    page = await context.new_page()
    await page.route("**/*", block_resources)

    if manual_login:
        logger.info("Manual login selected. Log in & press Enter in console.")
        input("Press Enter after manual login...")
        logger.info("User done with manual login.")
    else:
        creds_pool = config.get('linkedin_credentials', {}).values()
        if not creds_pool:
            logger.error("No LinkedIn creds in config! Aborting.")
            return
        creds = random.choice(list(creds_pool))
        username, password = creds['username'], creds['password']
        logger.info(f"Auto-login with => {username}")
        if not await ensure_logged_in(page, username, password):
            logger.error("LinkedIn auto-login failed. Aborting scrape.")
            return
        logger.info("Successfully logged in.")

    time_filter = config.get("time_filter", "r604800")
    scraper = LinkedInScraper(config, time_filter, blacklist)
    max_parallel = config.get("concurrent_details", 1)
    semaphore = asyncio.Semaphore(max_parallel)

    for query in search_queries:
        kw = query["keywords"]
        loc = query["location"]
        remote = query.get("remote")
        logger.info(f"Scraping => kw={kw}, loc={loc}, remote={remote}")
        job_infos = await scraper.search_jobs(page, kw, loc, remote)
        detail_tasks = []
        for info in job_infos:
            # Use scraper.base_url for consistency
            job_url = f"{scraper.base_url}/jobs/view/{info['job_id']}/"
            if job_exists(conn, job_url):
                logger.debug(f"Skipping existing => {job_url}")
                continue
            detail_tasks.append(fetch_job_detail_task(scraper, info, conn, page.context, blacklist, semaphore))
        results = await asyncio.gather(*detail_tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, dict):
                yield r

    try:
        logger.info("Navigating to LinkedIn feed for final cleanup.")
        await page.goto("https://www.linkedin.com/feed/", wait_until=scraper.wait_until)
        await simulate_human_behavior(page)
    except Exception:
        pass

    await context.close()
    conn.close()
    logger.info("Finished jobcards scraping. DB connection closed.")


async def fetch_job_detail_task(
        scraper: LinkedInScraper,
        info: Dict[str, Any],
        conn: Any,
        parent_context: Any,
        blacklist: Dict[str, set],
        sem: asyncio.Semaphore
) -> Optional[Dict[str, Any]]:
    """
    Concurrently fetch job detail for each job card.
    Applies blacklist checks and inserts the job into the database if valid.

    :param scraper: Instance of LinkedInScraper.
    :param info: Dictionary containing basic job info.
    :param conn: Database connection.
    :param parent_context: Browser context.
    :param blacklist: Dictionary with blacklist and whitelist sets.
    :param sem: Concurrency semaphore.
    :return: Dictionary with detailed job data if successful, else None.
    """
    async with sem:
        page = await parent_context.new_page()
        await page.route("**/*", block_resources)
        try:
            job_id = info['job_id']
            job_data = await scraper.get_job_details(page, job_id, search_card_info=info)
            if not job_data:
                return None
            title_lower = job_data['title'].lower()
            if any(bk in title_lower for bk in blacklist["blacklist"]) and not any(wk in title_lower for wk in blacklist["whitelist"]):
                logger.info(f"Skipping blacklisted => {job_data['title']}")
                return None
            if (
                    job_data.get('company_url', "").lower() in [x.lower() for x in blacklist["blacklist"]] or
                    job_data['company'].lower() in [x.lower() for x in blacklist["blacklist"]] or
                    job_data['job_url'].lower() in [x.lower() for x in blacklist["blacklist"]] or
                    is_company_blacklisted(conn, job_data['company'], job_data.get('company_url', ''))
            ):
                logger.info(f"Skipping blacklisted => {job_data['title']} at {job_data['company']}")
                return None
            job_data['company_size_score'] = get_company_size_score(job_data['company_size'])
            insert_job(conn, job_data)
            conn.commit()
            logger.info(f"Inserted new job => {job_data['title']} @ {job_data['company']}")
            return job_data
        except Exception as e:
            logger.error(f"Error in fetch_job_detail_task => {e}")
            return None
        finally:
            await page.close()


async def main_scraper(args: argparse.Namespace) -> None:
    """
    Main entry point to run scraping and/or processing.

    :param args: Parsed command-line arguments.
    """
    config = load_config("jobfuq/conf/config.toml")
    if args.hours is not None:
        seconds = args.hours * 3600
        config["time_filter"] = f"r{seconds}"
        logger.info(f"Using time filter for last {args.hours} hours => {config['time_filter']}")
    else:
        config.setdefault("time_filter", "r604800")
    config["manual_login"] = args.manual_login
    squeries = config.get('search_queries', [{'keywords': 'DevOps Engineer', 'location': 'Israel', 'remote': None}])

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=50)

        if args.debug_single:
            logger.info("Debug mode enabled: Scraping a single job and exiting.")
            conn = create_connection(config)
            create_table(conn)
            create_blacklist_table(conn)
            try:
                blacklist = load_blacklist(conn)
            except Exception as e:
                logger.error(f"Error loading blacklist: {e}")
                blacklist = {"blacklist": set(), "whitelist": set()}
            context = await browser.new_context(
                viewport={"width": 1280 + random.randint(-50, 50), "height": 720 + random.randint(-30, 30)},
                user_agent=random.choice(config.get('user_agents', ["Mozilla/5.0"]))
            )
            page = await context.new_page()
            await page.route("**/*", block_resources)
            await simulate_human_behavior(page)

            if args.manual_login:
                logger.info("Manual login selected for debug mode. Log in & press Enter in console.")
                input("Press Enter after manual login...")
                logger.info("User done with manual login.")
            else:
                creds_pool = config.get('linkedin_credentials', {}).values()
                if not creds_pool:
                    logger.error("No LinkedIn creds in config! Aborting debug mode.")
                    return
                creds = random.choice(list(creds_pool))
                username, password = creds['username'], creds['password']
                logger.info(f"Auto-login with => {username}")
                if not await ensure_logged_in(page, username, password):
                    logger.error("LinkedIn auto-login failed. Aborting debug mode.")
                    return
                logger.info("Successfully logged in.")
            query = squeries[0]
            kw = query["keywords"]
            loc = query["location"]
            remote = query.get("remote", None)
            logger.info(f"Debug mode: Scraping a single job for query => kw={kw}, loc={loc}, remote={remote}")
            scraper = LinkedInScraper(config, config.get("time_filter", "r604800"), blacklist)
            job_infos = await scraper.search_jobs(page, kw, loc, remote)
            if job_infos:
                info = job_infos[0]
                logger.info(f"Debug mode: Found job info for job_id={info['job_id']}")
                job_data = await scraper.get_job_details(page, info["job_id"], search_card_info=info)
                if job_data:
                    logger.info("Debug mode: Extracted job details:\n" + json.dumps(job_data, indent=2))
                else:
                    logger.error("Debug mode: Failed to extract job details.")
            else:
                logger.error("Debug mode: No job info found for query.")
            await context.close()
            conn.close()
            await browser.close()
            return

        recipe = args.recipe.split(",") if args.recipe else ["scrap", "process"]
        tasks = []
        if "scrap" in recipe:
            tasks.append(asyncio.create_task(run_scrape(config, browser, squeries, args.manual_login, endless=args.endless)))
        if "process" in recipe:
            threads = config.get("threads", 4)
            tasks.append(asyncio.create_task(process_and_rank_jobs(config, args.verbose, threads)))
        if tasks:
            if not args.endless:
                await asyncio.gather(*tasks)
                await asyncio.sleep(10)
                for t in tasks:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        logger.info("Task cancelled after single-round run.")
            else:
                await asyncio.gather(*tasks)
        await browser.close()
    logger.info("Scraping and processing complete. Browser closed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Advanced LinkedIn Scraper: robust pagination, dynamic selectors, and configurable parameters.",
        add_help=False
    )
    parser.add_argument("-h", "--hours", type=int, help="Time filter in hours (e.g., 6, 12, 24).", default=None)
    parser.add_argument("--manual-login", action="store_true", help="Manual login.")
    parser.add_argument("--recipe", type=str, default="scrap,process", help="Run mode: 'scrap', 'process', or 'scrap,process'.")
    parser.add_argument("--endless", action="store_true", help="Scrape continuously in a loop.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging.")
    parser.add_argument("--debug-single", action="store_true", help="Scrape a single job and exit for debugging purposes.")
    parser.add_argument("--help", action="help", help="Show this help message.")
    args = parser.parse_args()

    set_verbose(args.verbose)
    asyncio.run(main_scraper(args))
