import sys
import asyncio
import argparse
import json
import math
import os
import random
import re
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from jobfuq.logger import logger, set_verbose
from jobfuq.linked_utils import (
    ensure_logged_in,
    simulate_human_behavior,
    get_company_size,
    get_company_size_score,
    block_resources
)
from jobfuq.utils import load_config
from jobfuq.database import (
    create_connection,
    create_table,
    create_blacklist_table,
    load_blacklist,
    insert_job,
    job_exists,
    is_company_blacklisted
)
from jobfuq.processor import process_and_rank_jobs
from jobfuq.llm_handler import AIModel


class LinkedInScraper:
    """
    LinkedInScraper handles searching for jobs, extracting basic job-card info,
    and getting detailed job info from the job detail page.
    """

    def __init__(
            self,
            config: Dict[str, Any],
            time_filter: str,
            blacklist_data: Dict[str, Any]
    ):
        self.config = config
        self.time_filter = time_filter
        self.blacklist_data = blacklist_data
        try:
            self.scraper_config = load_config("jobfuq/conf/linked_config.toml")
            logger.info("Loaded scraper configuration from linked_config.toml")
        except Exception as e:
            logger.error(f"Failed to load scraper configuration: {e}")
            self.scraper_config = {}

        urls_conf = self.scraper_config.get("urls", {})
        timeouts_conf = self.scraper_config.get("timeouts", {})
        attrs_conf = self.scraper_config.get("attributes", {})

        self.base_url = urls_conf.get("base_url", "https://www.linkedin.com")
        self.wait_until = urls_conf.get("wait_until", "domcontentloaded")
        self.selector_timeout = timeouts_conf.get("selector_timeout", 30000)
        self.text_timeout = timeouts_conf.get("get_text_timeout", 5000)
        self.job_id_attrs = attrs_conf.get(
            "job_id", ["data-occludable-job-id", "data-job-id", "data-id"]
        )

        # Load scraping mode from the main config (jobfuq/conf/config.toml)
        self.scraping_mode = self.config.get("scraping", {}).get("mode", "normal").lower()
        if self.scraping_mode == "aggressive":
            logger.info("Aggressive mode active: reducing text timeout for fast extraction")
            self.text_timeout = 1000  # 1 second timeout

        self.company_size_cache: Dict[str, str] = {}

    async def search_jobs(
            self, page: Any, keywords: str, location: str, remote: Optional[bool] = None
    ) -> List[Dict[str, Any]]:
        """
        Search for LinkedIn jobs using base_url + search pattern,
        then paginate or scroll as needed to gather job cards.
        """
        search_pattern = self.scraper_config.get("urls", {}).get(
            "search_url_pattern",
            "/jobs/search/?keywords={keywords}&location={location}&f_TPR={time_filter}"
        )
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
        max_postings: int = self.config.get("max_postings", 100)

        while len(job_infos) < max_postings:
            current_url = page.url
            logger.info(f"Current page => {current_url}")

            new_infos = await self.extract_job_infos(page)
            job_infos.extend(new_infos)
            # Deduplicate
            job_infos = list({x["job_id"]: x for x in job_infos}.values())
            logger.info(f"Total job infos so far => {len(job_infos)}")

            if len(job_infos) >= max_postings:
                break

            old_count = len(job_infos)
            # Basic scroll
            await page.evaluate("window.scrollBy(0, 5000)")
            await asyncio.sleep(random.uniform(2, 3))

            new_infos_2 = await self.extract_job_infos(page)
            job_infos.extend(new_infos_2)
            job_infos = list({x["job_id"]: x for x in job_infos}.values())

            # If no new postings after scroll, try pagination
            if len(job_infos) == old_count:
                logger.info("No new postings after scroll. Attempting pagination.")
                success = await self.go_to_next_page(page, page_num)
                if not success:
                    logger.info("Pagination failed; assuming no more pages.")
                    break
                if page.url == current_url:
                    logger.info("Page URL unchanged after pagination; no more pages available.")
                    break
                page_num += 1

        return job_infos[:max_postings]

    async def fetch_applicants_count(self, context_obj: Any) -> Optional[int]:
        """
        Extract the number of applicants using a robust fallback strategy.
        Accepts either a card element or a full page.
        """
        au_conf = self.scraper_config.get("applicants_update", {})
        selectors = au_conf.get("selectors", [])
        xpaths = au_conf.get("xpaths", [])
        patterns = au_conf.get("patterns", [])

        # Try extracting using CSS selectors.
        for sel in selectors:
            try:
                elem = await context_obj.query_selector(sel)
                if elem:
                    text = (await elem.text_content() or "").strip()
                    for pattern in patterns:
                        match = re.search(pattern, text, re.IGNORECASE)
                        if match:
                            return int(match.group(1))
            except Exception:
                continue

        # Try extracting using XPath selectors.
        for xpath in xpaths:
            try:
                elem = await context_obj.query_selector(f"xpath={xpath}")
                if elem:
                    text = (await elem.text_content() or "").strip()
                    for pattern in patterns:
                        match = re.search(pattern, text, re.IGNORECASE)
                        if match:
                            return int(match.group(1))
            except Exception:
                continue

        # Fallback: scan full text of the context (card)
        try:
            full_text = (await context_obj.text_content() or "").strip()
            for pattern in patterns:
                match = re.search(pattern, full_text, re.IGNORECASE)
                if match:
                    return int(match.group(1))
        except Exception:
            pass

        # Final fallback: use full page text.
        try:
            page = context_obj.page if hasattr(context_obj, "page") else context_obj
            full_page_text = await page.evaluate("document.body.innerText")
            for pattern in patterns:
                match = re.search(pattern, full_page_text, re.IGNORECASE)
                if match:
                    return int(match.group(1))
        except Exception:
            pass

        logger.warning("❌ No applicants count found.")
        return None

    async def extract_applicants_count(self, card: Any) -> Optional[int]:
        """
        Delegates to fetch_applicants_count to extract applicants count from a card.
        """
        return await self.fetch_applicants_count(card)

    async def extract_job_infos(self, page: Any) -> List[Dict[str, Any]]:
        """
        Extract minimal info from job cards on the current page: ID, title, company, etc.
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

        # Blacklist and whitelist sets
        blacklist: set = self.blacklist_data.get("blacklist", set())
        whitelist: set = self.blacklist_data.get("whitelist", set())

        # Load selectors for card fields
        c = self.scraper_config.get("card", {})
        title_selectors = c.get("title_selectors", [])
        company_selectors = c.get("company_selectors", [])
        location_selectors = c.get("location_selectors", [])
        snippet_selectors = c.get("snippet_selectors", [])
        company_size_selectors = c.get("company_size_selectors", [])

        for card in job_cards:
            # Title extraction
            title_elem = None
            for sel in title_selectors:
                title_elem = await card.query_selector(sel)
                if title_elem:
                    break
            title = (await title_elem.text_content()).strip() if title_elem else ""
            logger.info(f"Extracted job title: '{title}'")

            title_lower = title.lower()
            if title and any(bk in title_lower for bk in blacklist) and not any(
                    wk in title_lower for wk in whitelist
            ):
                logger.info(f"Skipped blacklisted title => '{title}'")
                continue

            # Job ID extraction
            job_id = await self.extract_job_id(card)
            if not job_id:
                logger.warning(f"Skipping job due to missing job ID: '{title}'")
                continue

            # Company extraction
            company = ""
            for sel in company_selectors:
                elem = await card.query_selector(sel)
                if elem:
                    company = (await elem.text_content()).strip()
                    if company:
                        break

            # Location extraction
            loc = ""
            for sel in location_selectors:
                elem = await card.query_selector(sel)
                if elem:
                    loc = (await elem.text_content()).strip()
                    if loc:
                        break

            # Snippet/description extraction
            descr = ""
            for sel in snippet_selectors:
                elem = await card.query_selector(sel)
                if elem:
                    descr = (await elem.text_content()).strip()
                    if descr:
                        break

            # Applicants extraction using enhanced method
            applicants_count = await self.extract_applicants_count(card)

            # Company size extraction
            csize = None
            for sel in company_size_selectors:
                elem = await card.query_selector(sel)
                if elem:
                    txt = await elem.text_content()
                    if txt:
                        csize = txt.strip()
                        break

            job_info = {
                "job_id": job_id,
                "title": title,
                "company": company,
                "location": loc,
                "description": descr,
                "applicants_count": applicants_count,
                "company_size": csize or "Unknown",
            }
            results.append(job_info)

        logger.info(f"Extracted {len(results)} job cards (after blacklist check).")
        return results

    async def extract_job_id(self, card: Any) -> Optional[str]:
        """
        Extract the job ID from the card's attributes using the configured job_id_attrs.
        """
        for attr in self.job_id_attrs:
            val = await card.get_attribute(attr)
            if val:
                return val
        return None

    async def go_to_next_page(self, page: Any, current_page: int) -> bool:
        """
        Attempt to paginate to the next results page using the configured pagination selectors.
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
                    disabled = (await btn.get_attribute("disabled")) or (
                        await btn.get_attribute("aria-disabled")
                    )
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

    async def get_field_content(
            self, page: Any, selectors: List[str], default: str = ""
    ) -> str:
        """
        Return text content from the first found element among given selectors.
        """
        for sel in selectors:
            content = await self.get_text_content(page, sel, default="")
            if content:
                return content
        return default

    async def get_job_details(
            self, page: Any, job_id: str, search_card_info: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Navigate to the job detail URL and extract extended fields using configured selectors.
        """
        jurl = f"{self.base_url}/jobs/view/{job_id}/"
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
            # Note: Instead of using applicants_detail_selectors, we now always use the robust extraction.

            # Fallback from card info if available
            title = search_card_info["title"] if search_card_info else ""
            company = search_card_info["company"] if search_card_info else ""
            loc = search_card_info["location"] if search_card_info else ""
            descr = search_card_info["description"] if search_card_info else ""

            # If still missing, fetch from detail selectors
            if not title:
                title = await self.get_field_content(page, title_selectors, default="")
            if not company:
                company = await self.get_field_content(page, company_selectors, default="")
            if not loc:
                loc = await self.get_field_content(page, location_selectors, default="")
            if not descr:
                descr = await self.get_field_content(page, description_selectors, default="")

            posted_t = await self.get_field_content(page, posted_time_selectors, default="")
            date_str = self.parse_posting_date(posted_t)

            company_url = await self.get_field_content(page, company_url_selectors, default="")
            company_size = await self.get_field_content(page, company_size_selectors, default="Unknown")

            # Always use our robust applicants extraction for consistency with the debug script.
            applicants_count = await self.fetch_applicants_count(page)

            remote_flag = any(
                r in descr.lower()
                for r in ["remote", "wfh", "work from home", "work-from-home"]
            )

            job_data = {
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
                "job_state": "ACTIVE",
            }
            logger.info(f"Extracted job details: {job_data['title']} at {job_data['company']}")
            return job_data

        except Exception as e:
            logger.error(f"Failed to extract job details for job {job_id}: {e}")
            return None

    async def get_text_content(self, page: Any, selector: str, default: str = "") -> str:
        """
        Safely extract text content from the first element matching 'selector'.
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
        Convert posted time info (e.g. '1 day ago') to a 'YYYY-MM-DD' date string.
        """
        if not posted_time:
            return datetime.now().strftime("%Y-%m-%d")
        pt = posted_time.lower()
        try:
            if "minute" in pt or "hour" in pt or "just now" in pt:
                return datetime.now().strftime("%Y-%m-%d")
            elif "yesterday" in pt:
                return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            elif "day" in pt:
                days = int(re.search(r"(\d+)", pt).group())
                return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            elif "week" in pt:
                weeks = int(re.search(r"(\d+)", pt).group())
                return (datetime.now() - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
        except Exception as e:
            logger.error(f"Error parsing posting date: {e}")
        return datetime.now().strftime("%Y-%m-%d")

    def clean_html(self, txt: str) -> str:
        """
        Strip HTML tags and extra spaces from text.
        """
        if not txt:
            return ""
        c = re.sub(r"<[^>]+>", "", txt)
        return re.sub(r"\s+", " ", c).strip()

    async def update_existing_job(self, conn: Any, job_url: str, page: Any) -> Optional[Dict[str, Any]]:
        """
        Update job data (like applicants count) for an existing listing from the DB.
        Uses the enhanced extraction approach.
        """
        try:
            logger.info(f"Updating existing job: {job_url}")
            await page.goto(job_url, wait_until=self.wait_until, timeout=30000)
            await simulate_human_behavior(page)

            # Use the robust extraction method on the full page.
            applicants_count: Optional[int] = await self.fetch_applicants_count(page)

            # Determine application status based on text.
            not_accept = await page.query_selector("text=No longer accepting applications")
            accepting = not not_accept
            current_time = int(time.time() * 1000)
            status = "closed" if not accepting else "not applied"

            conn.execute(
                "UPDATE job_listings SET applicants_count = ?, last_checked = ?, application_status = ? WHERE job_url = ?",
                (applicants_count, current_time, status, job_url),
            )
            conn.commit()

            logger.info(
                f"Updated job {job_url}: applicants_count={applicants_count}, status={status}"
            )
            return {
                "job_url": job_url,
                "applicants_count": applicants_count,
                "application_status": status,
                "last_checked": current_time,
            }

        except Exception as e:
            logger.error(f"Error updating job {job_url}: {e}")
            return None


async def evaluate_job(ai_model: AIModel, job: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluate a job using AI model and merge the AI results with the original job data.
    """
    evaluation: Dict[str, Any] = await ai_model.evaluate_job_fit(job)
    return {**job, **evaluation}


async def run_scrape(
        config: Dict[str, Any],
        browser: Any,
        search_queries: List[Dict[str, Any]],
        manual_login: bool,
        endless: bool = False,
        args: Optional[argparse.Namespace] = None
) -> None:
    """
    Initiates scraping logic. If endless=True, it repeats forever with short breaks.
    """
    if endless:
        while True:
            async for job in get_jobcards(config, browser, search_queries, manual_login, args):
                pass
            logger.info("Scraping round complete. Waiting 60 sec before next round...")
            await asyncio.sleep(60)
    else:
        async for job in get_jobcards(config, browser, search_queries, manual_login, args):
            pass


async def get_jobcards(
        config: Dict[str, Any],
        browser: Any,
        search_queries: List[Dict[str, Any]],
        manual_login: bool,
        args: Optional[argparse.Namespace]
) -> Any:
    """
    Create context, log in, gather job listings from queries or debug_single mode, yields job data.
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
        user_agent=random.choice(config.get("user_agents", ["Mozilla/5.0"]))
    )
    page = await context.new_page()
    await page.route("**/*", block_resources)

    if manual_login:
        logger.info("Manual login selected. Log in & press Enter in console.")
        input("Press Enter after manual login...")
        logger.info("User done with manual login.")
    else:
        creds_pool = config.get("linkedin_credentials", {}).values()
        if not creds_pool:
            logger.error("No LinkedIn creds in config! Aborting.")
            return
        creds = random.choice(list(creds_pool))
        username, password = creds["username"], creds["password"]
        logger.info(f"Auto-login with => {username}")
        if not await ensure_logged_in(page, username, password):
            logger.error("LinkedIn auto-login failed. Aborting scrape.")
            return
        logger.info("Successfully logged in.")

    time_filter = config.get("time_filter", "r604800")
    scraper = LinkedInScraper(config, time_filter, blacklist)
    max_parallel = config.get("concurrent_details", 1)
    semaphore = asyncio.Semaphore(max_parallel)

    debug_job_url = None
    if args and args.debug_single and len(args.extra) > 0:
        debug_job_url = args.extra[0]

    if debug_job_url:
        logger.info(f"Debug mode: Using provided job URL => {debug_job_url}")
        job_data = None
        async with semaphore:
            detail_page = await context.new_page()
            await detail_page.route("**/*", block_resources)
            # The job_id is typically the second-to-last part of the URL
            try:
                split_url = debug_job_url.rstrip("/").split("/")
                job_id = split_url[-1] or split_url[-2]
                job_data = await scraper.get_job_details(detail_page, job_id)
            except Exception as e:
                logger.error(f"Could not parse job_id from debug link: {e}")
            await detail_page.close()
        if job_data:
            yield job_data
    else:
        for query in search_queries:
            kw = query["keywords"]
            loc = query["location"]
            remote = query.get("remote")
            logger.info(f"Scraping => kw={kw}, loc={loc}, remote={remote}")
            job_infos = await scraper.search_jobs(page, kw, loc, remote)

            # List to hold concurrent detail tasks
            tasks = []
            for info in job_infos:
                job_url = f"{scraper.base_url}/jobs/view/{info['job_id']}/"
                if job_exists(conn, job_url):
                    cursor = conn.execute(
                        "SELECT last_checked FROM job_listings WHERE job_url = ?",
                        (job_url,)
                    )
                    row = cursor.fetchone()
                    current_time = int(time.time() * 1000)
                    if row is None or row[0] is None or (current_time - row[0] > 86400000):
                        logger.info(f"Job {job_url} is older than a day; updating applicants count...")
                        update_page = await context.new_page()
                        await update_page.route("**/*", block_resources)
                        updated_job = await scraper.update_existing_job(conn, job_url, update_page)
                        await update_page.close()
                        if updated_job:
                            yield updated_job
                    else:
                        logger.debug(f"Job {job_url} exists and was recently checked; skipping update.")
                    continue

                # Create a task for fetching job details (the semaphore is used inside the task)
                tasks.append(asyncio.create_task(
                    fetch_job_detail_task(
                        scraper,
                        info,
                        conn,
                        page.context,
                        blacklist,
                        semaphore
                    )
                ))
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for detail in results:
                    if isinstance(detail, dict) and detail:
                        yield detail

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
    Runs in parallel to fetch job detail for each job card, apply blacklists,
    and insert into DB.
    """
    async with sem:
        page = await parent_context.new_page()
        await page.route("**/*", block_resources)
        try:
            job_id = info["job_id"]
            job_data = await scraper.get_job_details(page, job_id, search_card_info=info)
            if not job_data:
                return None

            title_lower = job_data["title"].lower()
            if (
                    any(bk in title_lower for bk in blacklist["blacklist"]) and
                    not any(wk in title_lower for wk in blacklist["whitelist"])
            ):
                logger.info(f"Skipping blacklisted => {job_data['title']}")
                return None

            if (
                    job_data.get("company_url", "").lower() in [x.lower() for x in blacklist["blacklist"]] or
                    job_data["company"].lower() in [x.lower() for x in blacklist["blacklist"]] or
                    job_data["job_url"].lower() in [x.lower() for x in blacklist["blacklist"]] or
                    is_company_blacklisted(conn, job_data["company"], job_data.get("company_url", ""))
            ):
                logger.info(
                    f"Skipping blacklisted => {job_data['title']} at {job_data['company']}"
                )
                return None

            job_data["company_size_score"] = get_company_size_score(job_data["company_size"])
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
    Main entry point. Reads config, sets up browser, runs either debug_single or
    normal workflow (scrap/process).
    """
    config = load_config("jobfuq/conf/config.toml")

    if args.hours is not None:
        seconds = args.hours * 3600
        config["time_filter"] = f"r{seconds}"
        logger.info(f"Using time filter for last {args.hours} hours => {config['time_filter']}")
    else:
        config.setdefault("time_filter", "r604800")

    config["manual_login"] = args.manual_login
    squeries = config.get(
        "search_queries",
        [{"keywords": "Master of Internet Surfing", "location": "Netherlands", "remote": None}]
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=50)

        if args.debug_single:
            logger.info("Debug mode enabled: Possibly scraping a single job link or one job from search.")
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
                user_agent=random.choice(config.get("user_agents", ["Mozilla/5.0"]))
            )
            page = await context.new_page()
            await page.route("**/*", block_resources)
            await simulate_human_behavior(page)

            if args.manual_login:
                logger.info("Manual login selected for debug mode. Log in & press Enter in console.")
                input("Press Enter after manual login...")
                logger.info("User done with manual login.")
            else:
                creds_pool = config.get("linkedin_credentials", {}).values()
                if not creds_pool:
                    logger.error("No LinkedIn creds in config! Aborting debug mode.")
                    return
                creds = random.choice(list(creds_pool))
                username, password = creds["username"], creds["password"]
                logger.info(f"Auto-login with => {username}")
                if not await ensure_logged_in(page, username, password):
                    logger.error("LinkedIn auto-login failed. Aborting debug mode.")
                    return
                logger.info("Successfully logged in.")

            if len(args.extra) > 0:
                job_link = args.extra[0]
                logger.info(f"Debug mode: Using provided job link => {job_link}")
                job_id = job_link.rstrip("/").split("/")[-1]
                if not job_id:
                    job_id = job_link.rstrip("/").split("/")[-2]
                scraper = LinkedInScraper(config, config.get("time_filter", "r604800"), blacklist)
                job_data = await scraper.get_job_details(page, job_id)
                if job_data:
                    logger.info("Debug mode: Extracted job details:\n" + json.dumps(job_data, indent=2))
                else:
                    logger.error("Debug mode: Failed to extract job details.")
            else:
                query = squeries[0]
                kw = query["keywords"]
                loc = query["location"]
                remote = query.get("remote", None)
                logger.info(
                    f"Debug mode: Scraping a single job for query => kw={kw}, loc={loc}, remote={remote}"
                )
                scraper = LinkedInScraper(config, config.get("time_filter", "r604800"), blacklist)
                job_infos = await scraper.search_jobs(page, kw, loc, remote)
                if job_infos:
                    info = job_infos[0]
                    logger.info(f"Debug mode: Found job info for job_id={info['job_id']}")
                    job_data = await scraper.get_job_details(page, info["job_id"], search_card_info=info)
                    if job_data:
                        logger.info(
                            "Debug mode: Extracted job details:\n" + json.dumps(job_data, indent=2)
                        )
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
            tasks.append(
                asyncio.create_task(
                    run_scrape(
                        config,
                        browser,
                        squeries,
                        args.manual_login,
                        endless=args.endless,
                        args=args
                    )
                )
            )
        if "process" in recipe:
            threads = config.get("threads", 4)
            tasks.append(
                asyncio.create_task(
                    process_and_rank_jobs(config, args.verbose, threads)
                )
            )
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
    parser.add_argument(
        "-h", "--hours",
        type=int,
        help="Time filter in hours (e.g., 6, 12, 24).",
        default=None
    )
    parser.add_argument(
        "--manual-login",
        action="store_true",
        help="Manual login."
    )
    parser.add_argument(
        "--recipe",
        type=str,
        default="scrap,process",
        help="Run mode: 'scrap', 'process', or 'scrap,process'."
    )
    parser.add_argument(
        "--endless",
        action="store_true",
        help="Scrape continuously in a loop."
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging."
    )
    parser.add_argument(
        "--debug-single",
        action="store_true",
        help="Scrape a single job and exit for debugging purposes."
    )
    parser.add_argument(
        "extra",
        nargs="*",
        help="Optional job URL for debug-single mode"
    )
    parser.add_argument(
        "--help",
        action="help",
        help="Show this help message."
    )
    args = parser.parse_args()
    set_verbose(args.verbose)
    asyncio.run(main_scraper(args))
