#!/usr/bin/env python3
"""
LinkedIn Scraper

This script scrapes LinkedIn job listings with advanced visual output using Rich.
It displays configuration flags, progress messages, live updates, and debug
information.

Usage:
    python scraper.py [OPTIONS]

Raises:
    RuntimeError: If critical configuration loading fails.
"""

import sys
import asyncio
import argparse
import json
import random
import re
import time
import sqlite3

from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.live import Live
from rich.text import Text

from jobfuq.logger import logger, set_verbose
from jobfuq.linked_utils import (
    ensure_logged_in,
    simulate_human_behavior,
    get_company_size,
    get_company_size_score,
    block_resources,
    handle_manual_captcha,
    wait_for_feed,
    load_session,
    rotate_session,
)
from jobfuq.utils import load_config
from jobfuq.database import (
    create_connection,
    create_table,
    create_blacklist_table,
    create_blacklisted_companies_table,
    load_blacklist,
    insert_job,
    job_exists,
    is_company_blacklisted,
)
from jobfuq.processor import process_and_rank_jobs
from jobfuq.llm_handler import AIModel


# =============================================================================
# GLOBAL VARIABLES & CONSTANTS
# =============================================================================

console: Console = Console()


# =============================================================================
# CONFIGURATION FLAGS DISPLAY
# =============================================================================
# ──► DISPLAY CONFIGURATION FLAGS WITH RICH
# =============================================================================

def print_config_flags(config: Dict[str, Any], args: argparse.Namespace) -> None:
    """
    Print the configuration flags with visual indicators using a Rich Table and Panel.

    Args:
        config (Dict[str, Any]): The configuration dictionary.
        args (argparse.Namespace): Parsed command-line arguments.

    Returns:
        None
    """
    flags: Dict[str, Any] = {
        "Manual Login": args.manual_login,
        "Debug Mode": args.debug_single or config.get("debug", {}).get("enabled", False),
        "Headless": config.get("headless", False),
        "Scraping Mode": config.get("scraping", {}).get("mode", "normal"),
        "Verbose": args.verbose or config.get("debug", {}).get("verbose", False),
    }

    table: Table = Table(
        show_header=True, header_style="bold magenta", expand=True, pad_edge=True
    )
    table.add_column("Configuration Flag", style="cyan", justify="left")
    table.add_column("Value", style="green", justify="right")

    for key, value in flags.items():
        mark: str = "[green]✅[/green]" if value else "[red]❌[/red]"
        table.add_row(key, f"{value} {mark}")

    panel: Panel = Panel(
        table, title="[bold blue]Configuration Flags[/bold blue]",
        border_style="bright_blue"
    )
    console.print(panel)


# =============================================================================
# LIVE STATUS UPDATER
# =============================================================================
# ──► CONTINUOUSLY UPDATE THE LIVE PANEL WITH SCRAPING STATUS
# =============================================================================

async def update_live_status(live: Live, status: Dict[str, int]) -> None:
    """
    Continuously update the live panel with the current scraping status.

    Args:
        live (Live): The Rich Live instance.
        status (Dict[str, int]): A dictionary with status info (e.g. jobs_scraped).

    Returns:
        None
    """
    while True:
        status_text = Text.assemble(
            ("🚀 Scraping Jobs...\n", "bold green"),
            (f"Jobs Scraped: {status.get('jobs_scraped', 0)}", "bold yellow")
        )
        live.update(
            Panel(
                status_text,
                title="[bold blue]Scraper Status[/bold blue]",
                border_style="bright_green"
            )
        )
        await asyncio.sleep(1)


# =============================================================================
# CORE SCRAPER METHODS
# =============================================================================
# ──► DATA EXTRACTION PATHWAY
# =============================================================================

class LinkedInScraper:
    """
    Class to handle scraping of LinkedIn job listings.

    Attributes:
        config (Dict[str, Any]): Main configuration dictionary.
        time_filter (str): Time filter parameter.
        blacklist_data (Dict[str, set]): Blacklist and whitelist data.
        playwright (Optional[Any]): Playwright instance (if provided).
        scraper_config (Dict[str, Any]): Additional scraper configuration.
        base_url (str): Base URL for LinkedIn.
        wait_until (str): Page load strategy.
        selector_timeout (int): Timeout for selectors.
        text_timeout (int): Timeout for text extraction.
        job_id_attrs (List[str]): Attributes to identify job IDs.
        scraping_mode (str): Mode for scraping (normal or aggressive).
        company_size_cache (Dict[str, Any]): Cache for company size data.
    """

    def __init__(
            self,
            config: Dict[str, Any],
            time_filter: str,
            blacklist_data: Dict[str, set],
            playwright: Optional[Any] = None,
    ) -> None:
        """
        Initialize the scraper with configuration, time filter, blacklist data, and
        an optional playwright instance.

        Args:
            config (Dict[str, Any]): Main configuration dictionary.
            time_filter (str): Time filter parameter.
            blacklist_data (Dict[str, set]): Blacklist and whitelist data.
            playwright (Optional[Any], optional): Playwright instance. Defaults to None.

        Raises:
            RuntimeError: If configuration loading fails.
        """
        self.config: Dict[str, Any] = config
        self.time_filter: str = time_filter
        self.blacklist_data: Dict[str, set] = blacklist_data
        self.playwright: Optional[Any] = playwright

        try:
            self.scraper_config: Dict[str, Any] = load_config("jobfuq/conf/linked_config.toml")
            logger.info("Loaded scraper config from linked_config.toml")
        except Exception as e:
            logger.error(f"Failed to load scraper config: {e}")
            self.scraper_config = {}

        urls_conf: Dict[str, Any] = self.scraper_config.get("urls", {})
        timeouts_conf: Dict[str, Any] = self.scraper_config.get("timeouts", {})
        attrs_conf: Dict[str, Any] = self.scraper_config.get("attributes", {})

        self.base_url: str = urls_conf.get("base_url", "https://www.linkedin.com")
        self.wait_until: str = urls_conf.get("wait_until", "domcontentloaded")
        self.selector_timeout: int = timeouts_conf.get("selector_timeout", 30000)
        self.text_timeout: int = timeouts_conf.get("get_text_timeout", 5000)
        self.job_id_attrs: List[str] = attrs_conf.get(
            "job_id", ["data-occludable-job-id", "data-job-id", "data-id"]
        )
        self.scraping_mode: str = config.get("scraping", {}).get("mode", "normal").lower()

        if self.scraping_mode == "aggressive":
            logger.info("Aggressive mode active: reducing text timeout")
            self.text_timeout = 1000

        self.company_size_cache: Dict[str, Any] = {}

    # -------------------------------------------------------------------------
    # SEARCH JOBS
    # -------------------------------------------------------------------------

    async def search_jobs(
            self, page: Any, keywords: str, location: str,
            remote: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search for jobs on LinkedIn based on keywords and location.

        Args:
            page (Any): The Playwright page instance.
            keywords (str): Search keywords.
            location (str): Job location.
            remote (Optional[Any], optional): Remote filter. Defaults to None.

        Returns:
            List[Dict[str, Any]]: A list of job information dictionaries.
        """
        sp: str = self.scraper_config.get("urls", {}).get(
            "search_url_pattern",
            "/jobs/search/?keywords={keywords}&location={location}&f_TPR={time_filter}",
        )
        search_url: str = self.base_url + sp.format(
            keywords=keywords, location=location, time_filter=self.time_filter
        )
        logger.info(f"Navigating to: {search_url}")
        await page.goto(search_url, wait_until=self.wait_until)

        # Handle captcha if detected
        checkpoint_indicator: str = self.scraper_config.get("urls", {}).get(
            "checkpoint_indicator", "checkpoint/challenge"
        )
        if checkpoint_indicator in page.url:
            page = await handle_manual_captcha(page, self.playwright, self.config)

        logger.info(f"Landed on: {page.url}")
        await simulate_human_behavior(page)

        for sel in self.scraper_config.get("jobs", {}).get("job_list_selectors", []):
            try:
                await page.wait_for_selector(sel, timeout=self.selector_timeout)
                logger.info(f"Job list found: {sel}")
                break
            except PlaywrightTimeoutError:
                logger.debug(f"No job list with {sel}")

        job_infos: List[Dict[str, Any]] = []
        page_num: int = 1
        max_postings: int = self.config.get("max_postings", 100)

        while len(job_infos) < max_postings:
            logger.info(f"Current page: {page.url}")
            job_infos.extend(await self.extract_job_infos(page))
            job_infos = list({x["job_id"]: x for x in job_infos}.values())
            logger.info(f"Total jobs so far: {len(job_infos)}")

            if len(job_infos) >= max_postings:
                break

            old_count: int = len(job_infos)
            await page.evaluate("window.scrollBy(0, 5000)")
            await asyncio.sleep(random.uniform(2, 3))
            job_infos.extend(await self.extract_job_infos(page))
            job_infos = list({x["job_id"]: x for x in job_infos}.values())

            if len(job_infos) == old_count:
                logger.info("No new postings; attempting pagination.")
                if not await self.go_to_next_page(page, page_num):
                    logger.info("Pagination failed / no more pages.")
                    break
                if page.url == page.url:
                    break
                page_num += 1

        return job_infos[:max_postings]

    # -------------------------------------------------------------------------
    # PAGINATION
    # -------------------------------------------------------------------------

    async def go_to_next_page(self, page: Any, current_page: int) -> bool:
        """
        Navigate to the next page of job listings.

        Args:
            page (Any): The Playwright page instance.
            current_page (int): The current page number.

        Returns:
            bool: True if navigation was successful, False otherwise.
        """
        next_num: int = current_page + 1
        logger.info(f"Next page: {next_num}")

        for sel in self.scraper_config.get("pagination", {}).get("selectors", []):
            try:
                try:
                    formatted_sel: str = sel.format(page=next_num)
                except Exception:
                    formatted_sel = sel

                btn: Any = await page.query_selector(formatted_sel)
                if btn:
                    disabled: Optional[str] = (
                            await btn.get_attribute("disabled")
                            or await btn.get_attribute("aria-disabled")
                    )
                    if disabled and disabled.lower() in ["true", "disabled"]:
                        logger.info(f"Button '{formatted_sel}' disabled.")
                        continue
                    logger.info(f"Clicking pagination: '{formatted_sel}'")
                    await btn.click()
                    await page.wait_for_load_state(self.wait_until)
                    await asyncio.sleep(2)
                    return True
            except Exception as e:
                logger.debug(f"Error with pagination '{formatted_sel}': {e}")

        logger.info(f"No valid pagination for page {next_num}.")
        return False

    # -------------------------------------------------------------------------
    # JOB INFORMATION EXTRACTION
    # -------------------------------------------------------------------------

    async def extract_job_infos(self, page: Any) -> List[Dict[str, Any]]:
        """
        Extract job information from the job cards on the current page.

        Args:
            page (Any): The Playwright page instance.

        Returns:
            List[Dict[str, Any]]: A list of job information dictionaries.
        """
        results: List[Dict[str, Any]] = []
        logger.info("Extracting job cards...")
        card_selectors: List[str] = self.scraper_config.get("jobs", {}).get(
            "job_card_selectors", []
        )
        job_cards: List[Any] = []

        for sel in card_selectors:
            try:
                found: List[Any] = await page.query_selector_all(sel)
                if found:
                    job_cards = found
                    logger.info(f"Found {len(found)} cards using: {sel}")
                    break
            except Exception as ex:
                logger.debug(f"Error with {sel}: {ex}")

        if not job_cards:
            logger.debug("No job cards found.")
            return results

        blacklist: set = self.blacklist_data.get("blacklist", set())
        whitelist: set = self.blacklist_data.get("whitelist", set())
        c: Dict[str, Any] = self.scraper_config.get("card", {})
        title_selectors: List[str] = c.get("title_selectors", [])
        company_selectors: List[str] = c.get("company_selectors", [])
        location_selectors: List[str] = c.get("location_selectors", [])
        snippet_selectors: List[str] = c.get("snippet_selectors", [])
        company_size_selectors: List[str] = c.get("company_size_selectors", [])

        for card in job_cards:
            title_elem: Any = None
            for sel in title_selectors:
                title_elem = await card.query_selector(sel)
                if title_elem:
                    break
            title: str = (await title_elem.text_content()).strip() if title_elem else ""
            logger.info(f"Extracted title: '{title}'")

            if title and any(bk in title.lower() for bk in blacklist) and not any(
                    wk in title.lower() for wk in whitelist
            ):
                logger.info(f"Skipped blacklisted: '{title}'")
                continue

            job_id: Optional[str] = await self.extract_job_id(card)
            if not job_id:
                logger.warning(f"Missing job ID for: '{title}'")
                continue

            company: str = ""
            for sel in company_selectors:
                elem: Any = await card.query_selector(sel)
                if elem:
                    company = (await elem.text_content()).strip()
                    if company:
                        break

            loc: str = ""
            for sel in location_selectors:
                elem = await card.query_selector(sel)
                if elem:
                    loc = (await elem.text_content()).strip()
                    if loc:
                        break

            descr: str = ""
            for sel in snippet_selectors:
                elem = await card.query_selector(sel)
                if elem:
                    descr = (await elem.text_content()).strip()
                    if descr:
                        break

            applicants_count: Optional[int] = await self.extract_applicants_count(card)
            csize: Optional[str] = None
            for sel in company_size_selectors:
                elem = await card.query_selector(sel)
                if elem:
                    txt: Optional[str] = await elem.text_content()
                    if txt:
                        csize = txt.strip()
                        break

            results.append(
                {
                    "job_id": job_id,
                    "title": title,
                    "company": company,
                    "location": loc,
                    "description": descr,
                    "applicants_count": applicants_count,
                    "company_size": csize or "Unknown",
                }
            )

        logger.info(f"Extracted {len(results)} job cards.")
        return results

    # -------------------------------------------------------------------------
    # HELPER METHODS
    # -------------------------------------------------------------------------

    async def extract_job_id(self, card: Any) -> Optional[str]:
        """
        Extract the job ID from a job card using defined attributes.

        Args:
            card (Any): The job card element.

        Returns:
            Optional[str]: The extracted job ID if found; otherwise, None.
        """
        for attr in self.job_id_attrs:
            val: Optional[str] = await card.get_attribute(attr)
            if val:
                return val
        return None

    async def extract_applicants_count(self, card: Any) -> Optional[int]:
        """
        Extract the applicants count from a job card.

        Args:
            card (Any): The job card element.

        Returns:
            Optional[int]: The number of applicants if found; otherwise, None.
        """
        return await self.fetch_applicants_count(card)

    async def fetch_applicants_count(self, context_obj: Any) -> Optional[int]:
        """
        Fetch the number of applicants from the provided context using selectors
        and patterns.

        Args:
            context_obj (Any): The element or page from which to extract data.

        Returns:
            Optional[int]: The applicants count if found; otherwise, None.
        """
        au_conf: Dict[str, Any] = self.scraper_config.get("applicants_update", {})
        selectors: List[str] = au_conf.get("selectors", [])
        xpaths: List[str] = au_conf.get("xpaths", [])
        patterns: List[str] = au_conf.get("patterns", [])

        for sel in selectors:
            try:
                elem: Any = await context_obj.query_selector(sel)
                if elem:
                    text: str = (await elem.text_content() or "").strip()
                    for pattern in patterns:
                        match = re.search(pattern, text, re.IGNORECASE)
                        if match:
                            return int(match.group(1))
            except Exception:
                continue

        for xpath in xpaths:
            try:
                elem: Any = await context_obj.query_selector(f"xpath={xpath}")
                if elem:
                    text: str = (await elem.text_content() or "").strip()
                    for pattern in patterns:
                        match = re.search(pattern, text, re.IGNORECASE)
                        if match:
                            return int(match.group(1))
            except Exception:
                continue

        try:
            full_text: str = (await context_obj.text_content() or "").strip()
            for pattern in patterns:
                match = re.search(pattern, full_text, re.IGNORECASE)
                if match:
                    return int(match.group(1))
        except Exception:
            pass

        try:
            page: Any = getattr(context_obj, "page", None) or context_obj
            full_page_text: str = await page.evaluate("document.body.innerText")
            for pattern in patterns:
                match = re.search(pattern, full_page_text, re.IGNORECASE)
                if match:
                    return int(match.group(1))
        except Exception:
            pass

        logger.warning("❌ No applicants count found.")
        return None

    async def get_job_details(
            self,
            page: Any,
            job_id: str,
            conn: Optional[sqlite3.Connection] = None,
            **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieve detailed job information from a specific job listing.

        Args:
            page (Any): The Playwright page instance.
            job_id (str): The job ID.
            conn (Optional[sqlite3.Connection], optional): Database connection.
                Defaults to None.
            **kwargs: Additional keyword arguments.

        Returns:
            Optional[Dict[str, Any]]: A dictionary with job details or None on failure.
        """
        jurl: str = f"{self.base_url}/jobs/view/{job_id}/"
        logger.info(f"Job detail: {jurl}")

        try:
            await page.goto(jurl, wait_until=self.wait_until)
            await simulate_human_behavior(page)
        except PlaywrightTimeoutError:
            logger.error(f"Timeout for job {job_id}")
            return None
        except Exception as e:
            logger.error(f"Error for job {job_id}: {e}")
            return None

        closed_app_text: str = self.scraper_config.get("applicants_update", {}).get(
            "closed_application_text", "no longer accepting applications"
        ).lower()
        feedback_elem: Any = await page.query_selector(".artdeco-inline-feedback__message")
        if feedback_elem:
            feedback_text: str = (await feedback_elem.text_content() or "").strip().lower()
            if closed_app_text in feedback_text:
                logger.info(f"Job {job_id} is closed (no longer accepting applications).")
                if conn:
                    current_time: int = int(time.time() * 1000)
                    conn.execute(
                        "UPDATE job_listings SET applicants_count = ?, last_checked = ?, "
                        "application_status = ?, job_state = ? WHERE job_url = ?",
                        (999, current_time, "closed", "CLOSED", jurl),
                    )
                    conn.commit()
                return None

        detail_conf: Dict[str, Any] = self.scraper_config.get("detail", {})
        title_selectors: List[str] = detail_conf.get("title_selectors", [])
        company_selectors: List[str] = detail_conf.get("company_selectors", [])
        location_selectors: List[str] = detail_conf.get("location_selectors", [])
        description_selectors: List[str] = detail_conf.get("description_selectors", [])
        company_size_selectors: List[str] = detail_conf.get("company_size_selectors", [])

        title: str = await self.get_field_content(page, title_selectors, default="")
        company: str = await self.get_field_content(page, company_selectors, default="")
        loc: str = await self.get_field_content(page, location_selectors, default="")
        descr: str = await self.get_field_content(page, description_selectors, default="")
        applicants_count: Optional[int] = await self.fetch_applicants_count(page)
        company_size: str = await self.get_field_content(page, company_size_selectors, default="Unknown")

        job_data: Dict[str, Any] = {
            "job_id": job_id,
            "title": title.strip(),
            "company": company.strip(),
            "company_url": "",
            "location": loc.strip(),
            "description": self.clean_html(descr.strip()),
            "remote_allowed": False,
            "job_state": "ACTIVE",
            "company_size": company_size,
            "company_size_score": 0,
            "job_url": jurl,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "listed_at": int(time.time() * 1000),
            "applicants_count": applicants_count,
            "overall_relevance": 0.0,
            "is_posted": 1,
            "application_status": "not applied",
        }
        logger.info(f"Extracted details: {job_data['title']} @ {job_data['company']}")
        return job_data

    async def get_field_content(
            self, page: Any, selectors: List[str], default: str = ""
    ) -> str:
        """
        Retrieve text content from a list of selectors, returning the longest found.

        Args:
            page (Any): The Playwright page instance.
            selectors (List[str]): List of selectors.
            default (str, optional): Default value if none found. Defaults to "".

        Returns:
            str: The retrieved text content.
        """
        desc_text: str = ""
        for sel in selectors:
            content: str = await self.get_text_content(page, sel, default="")
            if content and len(content) > len(desc_text):
                desc_text = content
        return desc_text if desc_text else default

    async def get_text_content(
            self, page: Any, selector: str, default: str = ""
    ) -> str:
        """
        Get trimmed text content for a given selector with a timeout.

        Args:
            page (Any): The Playwright page instance.
            selector (str): The CSS selector.
            default (str, optional): Default value if not found. Defaults to "".

        Returns:
            str: The text content.
        """
        try:
            el: Any = await page.wait_for_selector(selector, timeout=self.text_timeout)
            if el:
                txt: Optional[str] = await el.text_content()
                if txt:
                    return txt.strip()
        except PlaywrightTimeoutError:
            pass
        except Exception as e:
            logger.debug(f"get_text_content error: {e}")
        return default

    def parse_posting_date(self, posted_time: str) -> str:
        """
        Parse a human-readable posting date into a standard date format.

        Args:
            posted_time (str): The posting time in human-readable format.

        Returns:
            str: The posting date in "%Y-%m-%d" format.
        """
        if not posted_time:
            return datetime.now().strftime("%Y-%m-%d")

        pt: str = posted_time.lower()
        try:
            if "minute" in pt or "hour" in pt or "just now" in pt:
                return datetime.now().strftime("%Y-%m-%d")
            if "yesterday" in pt:
                return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            if "day" in pt:
                days: int = int(re.search(r"(\d+)", pt).group())
                return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            if "week" in pt:
                weeks: int = int(re.search(r"(\d+)", pt).group())
                return (datetime.now() - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
        except Exception as e:
            logger.error(f"Error parsing date: {e}")
        return datetime.now().strftime("%Y-%m-%d")

    def clean_html(self, txt: str) -> str:
        """
        Remove HTML tags and extra whitespace from text.

        Args:
            txt (str): The HTML text.

        Returns:
            str: Cleaned text.
        """
        if not txt:
            return ""
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", txt)).strip()

    async def update_existing_job(
            self, conn: sqlite3.Connection, job_url: str, page: Any
    ) -> Optional[Dict[str, Any]]:
        """
        Update job details if the job listing already exists in the database.

        Args:
            conn (sqlite3.Connection): The database connection.
            job_url (str): The job URL.
            page (Any): The Playwright page instance.

        Returns:
            Optional[Dict[str, Any]]: A dictionary with updated job info or None on failure.
        """
        try:
            logger.info(f"Updating job: {job_url}")
            await page.goto(job_url, wait_until=self.wait_until, timeout=30000)
            await simulate_human_behavior(page)
            closed_app_text: str = self.scraper_config.get("applicants_update", {}).get(
                "closed_application_text", "no longer accepting applications"
            ).lower()
            feedback_elem: Any = await page.query_selector(".artdeco-inline-feedback__message")
            if feedback_elem:
                feedback_text: str = (await feedback_elem.text_content() or "").strip().lower()
                if closed_app_text in feedback_text:
                    logger.info(f"Job {job_url} is closed. Marking in DB as CLOSED.")
                    current_time: int = int(time.time() * 1000)
                    conn.execute(
                        "UPDATE job_listings SET applicants_count = ?, last_checked = ?, "
                        "application_status = ?, job_state = ? WHERE job_url = ?",
                        (999, current_time, "closed", "CLOSED", job_url),
                    )
                    conn.commit()
                    return {
                        "job_url": job_url,
                        "applicants_count": 999,
                        "job_state": "CLOSED",
                        "last_checked": current_time,
                    }
            applicants_count: Optional[int] = await self.fetch_applicants_count(page)
            current_time: int = int(time.time() * 1000)
            conn.execute(
                "UPDATE job_listings SET applicants_count = ?, last_checked = ?, job_state = ? "
                "WHERE job_url = ?",
                (applicants_count, current_time, "ACTIVE", job_url),
            )
            conn.commit()
            logger.info(f"Updated {job_url}: count={applicants_count}, state=ACTIVE")
            return {
                "job_url": job_url,
                "applicants_count": applicants_count,
                "job_state": "ACTIVE",
                "last_checked": current_time,
            }
        except Exception as e:
            logger.error(f"Error updating {job_url}: {e}")
            return None


# =============================================================================
# SCRAPING WORKFLOW FUNCTIONS
# =============================================================================
# ──► ORCHESTRATION OF JOB EVALUATION AND SCRAPING
# =============================================================================

async def evaluate_job(ai_model: AIModel, job: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluate job fit using the provided AI model.

    Args:
        ai_model (AIModel): The AI model instance.
        job (Dict[str, Any]): The job details.

    Returns:
        Dict[str, Any]: Combined job and evaluation data.
    """
    evaluation: Dict[str, Any] = await ai_model.evaluate_job_fit(job)
    return {**job, **evaluation}


async def run_scrape(
        config: Dict[str, Any],
        browser: Any,
        search_queries: List[Dict[str, Any]],
        manual_login: bool,
        endless: bool = False,
        args: Optional[argparse.Namespace] = None,
        playwright: Optional[Any] = None,
        status: Optional[Dict[str, int]] = None,
) -> None:
    """
    Run the scraping process, continuously if specified.

    Args:
        config (Dict[str, Any]): The configuration dictionary.
        browser (Any): The Playwright browser instance.
        search_queries (List[Dict[str, Any]]): List of search query parameters.
        manual_login (bool): Whether to perform manual login.
        endless (bool, optional): Run continuously if True. Defaults to False.
        args (Optional[argparse.Namespace], optional): Command-line arguments.
        playwright (Optional[Any], optional): Playwright instance.
        status (Optional[Dict[str, int]]): Dictionary for tracking live status.

    Returns:
        None
    """
    if endless:
        while True:
            async for job in get_jobcards(
                    config, browser, search_queries, manual_login, args, playwright, status=status
            ):
                pass
            logger.info("Round complete. Waiting 60 sec...")
            await asyncio.sleep(60)
    else:
        async for job in get_jobcards(
                config, browser, search_queries, manual_login, args, playwright, status=status
        ):
            pass


async def get_jobcards(
        config: Dict[str, Any],
        browser: Any,
        search_queries: List[Dict[str, Any]],
        manual_login: bool,
        args: argparse.Namespace,
        playwright: Optional[Any] = None,
        status: Optional[Dict[str, int]] = None,
) -> Any:
    """
    Retrieve job cards by initiating a new browser context and handling login.

    Args:
        config (Dict[str, Any]): The configuration dictionary.
        browser (Any): The Playwright browser instance.
        search_queries (List[Dict[str, Any]]): Search query parameters.
        manual_login (bool): Whether to use manual login.
        args (argparse.Namespace): Command-line arguments.
        playwright (Optional[Any], optional): Playwright instance.
        status (Optional[Dict[str, int]]): Dictionary for tracking live status.

    Yields:
        Optional[Dict[str, Any]]: Detailed job data.
    """
    conn: sqlite3.Connection = create_connection(config)
    create_table(conn)
    create_blacklist_table(conn)
    create_blacklisted_companies_table(conn)

    try:
        blacklist: Dict[str, set] = load_blacklist(conn)
    except Exception as e:
        logger.error(f"Error loading blacklist: {e}")
        blacklist = {"blacklist": set(), "whitelist": set()}

    context = await browser.new_context(
        viewport={
            "width": 1280 + random.randint(-50, 50),
            "height": 720 + random.randint(-30, 30),
        },
        user_agent=random.choice(config.get("user_agents", ["Mozilla/5.0"])),
    )
    await context.route("**/*", block_resources)
    page = await context.new_page()

    if manual_login:
        login_url: str = load_config("jobfuq/conf/linked_config.toml").get(
            "urls", {}
        ).get("login_url", "https://www.linkedin.com/login")
        logger.info("Manual login selected. Navigating to LinkedIn login page. Please log in manually.")
        await page.goto(login_url, wait_until="networkidle")
        await asyncio.to_thread(input, "Press Enter after you have logged in...")
        new_page = await wait_for_feed(page, playwright, config)
        if not new_page:
            logger.warning("Feed did not load. Checking if a captcha page is open...")
            if "checkpoint/challenge" in page.url:
                page = await handle_manual_captcha(page, playwright, config)
            else:
                logger.error("Unknown issue: login completed but feed did not load.")
                return
        else:
            page = new_page
        logger.info("Manual login complete.")
    else:
        creds_pool = config.get("linkedin_credentials", {}).values()
        if not creds_pool:
            logger.error("No LinkedIn creds! Aborting.")
            return
        creds = random.choice(list(creds_pool))
        username, password = creds["username"], creds["password"]
        logger.info(f"Auto-login with => {username}")
        logged_in_page = await ensure_logged_in(page, username, password, playwright, config)
        if not logged_in_page:
            logger.error("Auto-login failed. Aborting.")
            return
        page = logged_in_page
        logger.info("Logged in.")

    time_filter: str = config.get("time_filter", "r604800")
    scraper = LinkedInScraper(config, time_filter, blacklist, playwright=playwright)
    max_parallel: int = config.get("concurrent_details", 1)
    semaphore = asyncio.Semaphore(max_parallel)
    debug_job_url: Optional[str] = (
        args.extra[0] if args.debug_single and args.extra and len(args.extra) > 0 else None
    )

    if debug_job_url:
        logger.info(f"Debug: Using job URL => {debug_job_url}")
        async with semaphore:
            detail_page = await context.new_page()
            await detail_page.route("**/*", block_resources)
            try:
                split_url = debug_job_url.rstrip("/").split("/")
                job_id: str = split_url[-1] or split_url[-2]
                job_data = await scraper.get_job_details(detail_page, job_id)
            except Exception as e:
                logger.error(f"Parse error: {e}")
                job_data = None
            await detail_page.close()
            if job_data:
                if status is not None:
                    status["jobs_scraped"] += 1
                yield job_data
    else:
        for query in search_queries:
            kw: str = query["keywords"]
            loc: str = query["location"]
            remote: Optional[Any] = query.get("remote")
            logger.info(f"Scraping => kw={kw}, loc={loc}, remote={remote}")
            job_infos = await scraper.search_jobs(page, kw, loc, remote)
            tasks: List[Any] = []
            for info in job_infos:
                job_url: str = f"{scraper.base_url}/jobs/view/{info['job_id']}/"
                if job_exists(conn, job_url):
                    cursor = conn.execute(
                        "SELECT last_checked FROM job_listings WHERE job_url = ?", (job_url,)
                    )
                    row = cursor.fetchone()
                    current_time: int = int(time.time() * 1000)
                    if row is None or row[0] is None or current_time - row[0] > 86400000:
                        logger.info(f"Job {job_url} outdated (>24h); updating...")
                        update_page = await context.new_page()
                        await update_page.route("**/*", block_resources)
                        updated_job = await scraper.update_existing_job(conn, job_url, update_page)
                        await update_page.close()
                        if updated_job:
                            if status is not None:
                                status["jobs_scraped"] += 1
                            yield updated_job
                    else:
                        logger.debug(f"Job {job_url} exists and is recent; skipping.")
                    continue
                tasks.append(
                    asyncio.create_task(
                        fetch_job_detail_task(
                            scraper, info, conn, context, blacklist, semaphore, status=status
                        )
                    )
                )
            if tasks:
                detailed = await asyncio.gather(*tasks, return_exceptions=True)
                for detail in detailed:
                    if isinstance(detail, dict) and detail:
                        if status is not None:
                            status["jobs_scraped"] += 1
                        yield detail

    try:
        logger.info("Navigating to LinkedIn feed for cleanup.")
        feed_url: str = load_config("jobfuq/conf/linked_config.toml").get(
            "urls", {}
        ).get("feed_url", "https://www.linkedin.com/feed/")
        await page.goto(feed_url, wait_until=scraper.wait_until)
        await simulate_human_behavior(page)
    except Exception:
        pass

    await context.close()
    conn.close()
    logger.info("Scraping complete. DB closed.")


async def fetch_job_detail_task(
        scraper: LinkedInScraper,
        info: Dict[str, Any],
        conn: sqlite3.Connection,
        parent_context: Any,
        blacklist: Dict[str, set],
        sem: asyncio.Semaphore,
        status: Optional[Dict[str, int]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Asynchronously fetch detailed information for a specific job.

    Args:
        scraper (LinkedInScraper): The scraper instance.
        info (Dict[str, Any]): Basic job information.
        conn (sqlite3.Connection): The database connection.
        parent_context (Any): Parent browser context.
        blacklist (Dict[str, set]): Blacklist and whitelist data.
        sem (asyncio.Semaphore): Semaphore for limiting concurrency.
        status (Optional[Dict[str, int]]): Dictionary for tracking live status.

    Returns:
        Optional[Dict[str, Any]]: Detailed job data if successful; otherwise, None.
    """
    async with sem:
        page = await parent_context.new_page()
        await page.route("**/*", block_resources)
        try:
            job_id: str = info["job_id"]
            job_data = await scraper.get_job_details(page, job_id, conn, search_card_info=info)
            if not job_data:
                return None

            title_lower: str = job_data["title"].lower()
            if any(bk in title_lower for bk in blacklist["blacklist"]) and not any(
                    wk in title_lower for wk in blacklist["whitelist"]
            ):
                logger.info(f"Skipping blacklisted job: {job_data['title']}")
                return None
            if (
                    job_data.get("company_url", "").lower() in [x.lower() for x in blacklist["blacklist"]]
                    or job_data["company"].lower() in [x.lower() for x in blacklist["blacklist"]]
                    or job_data["job_url"].lower() in [x.lower() for x in blacklist["blacklist"]]
                    or is_company_blacklisted(conn, job_data["company"], job_data.get("company_url", ""))
            ):
                logger.info(f"Skipping blacklisted: {job_data['title']} at {job_data['company']}")
                return None

            job_data["company_size_score"] = get_company_size_score(job_data["company_size"])
            logger.info(f"Saving job {job_data['job_id']} with desc: {job_data['description'][:500]}")
            insert_job(conn, job_data)
            conn.commit()
            logger.info(f"Inserted job: {job_data['title']} @ {job_data['company']}")
            if status is not None:
                status["jobs_scraped"] += 1
            return job_data
        except Exception as e:
            logger.error(f"Detail task error: {e}")
            return None
        finally:
            await page.close()


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================
# ──► INITIALIZE AND EXECUTE THE SCRAPER
# =============================================================================

async def main_scraper(args: argparse.Namespace) -> None:
    """
    Main scraper function to load configuration, handle debug modes, and start scraping.

    Args:
        args (argparse.Namespace): Command-line arguments.

    Returns:
        None
    """
    console.print("[bold blue]🚀 Starting LinkedIn Scraper[/bold blue]")
    config: Dict[str, Any] = load_config("jobfuq/conf/config.toml")
    if args.hours is not None:
        config["time_filter"] = f"r{args.hours * 3600}"
        logger.info(f"Time filter: {config['time_filter']}")
    else:
        config.setdefault("time_filter", "r604800")

    config["manual_login"] = args.manual_login
    squeries: List[Dict[str, Any]] = config.get(
        "search_queries",
        [
            {
                "keywords": "Master of Internet Surfing",
                "location": "Netherlands",
                "remote": None,
            }
        ],
    )
    headless_mode: bool = config.get("headless", False)
    logger.info(f"Launching browser with headless={headless_mode}")
    print_config_flags(config, args)

    status: Dict[str, int] = {"jobs_scraped": 0}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless_mode, slow_mo=50)
        live_panel = Panel(
            Text("Initializing..."),
            title="[bold blue]Scraper Status[/bold blue]",
            border_style="bright_green"
        )
        live_task = asyncio.create_task(
            update_live_status(Live(live_panel, refresh_per_second=4), status)
        )

        if args.debug_single or config.get("debug", {}).get("enabled", False):
            logger.info("Debug mode enabled.")
            dbg: Dict[str, Any] = config.get("debug", {})
            conn: sqlite3.Connection = create_connection(config)
            create_table(conn)
            create_blacklist_table(conn)
            create_blacklisted_companies_table(conn)
            try:
                blacklist: Dict[str, set] = load_blacklist(conn)
            except Exception as e:
                logger.error(f"Blacklist error: {e}")
                blacklist = {"blacklist": set(), "whitelist": set()}

            context = await browser.new_context(
                viewport={
                    "width": 1280 + random.randint(-50, 50),
                    "height": 720 + random.randint(-30, 30),
                },
                user_agent=random.choice(config.get("user_agents", ["Mozilla/5.0"])),
            )
            await context.route("**/*", block_resources)
            page = await context.new_page()
            await simulate_human_behavior(page)

            if args.manual_login:
                logger.info("Manual login for debug. Press Enter...")
                input("Press Enter after login...")
                logger.info("Login complete.")
            else:
                creds_pool = config.get("linkedin_credentials", {}).values()
                if not creds_pool:
                    logger.error("No credentials! Aborting debug.")
                    return
                creds = random.choice(list(creds_pool))
                username, password = creds["username"], creds["password"]
                logger.info(f"Auto-login with => {username}")
                logged_in_page = await ensure_logged_in(page, username, password, p, config)
                if not logged_in_page:
                    logger.error("Auto-login failed. Aborting debug.")
                    return
                page = logged_in_page
                logger.info("Logged in.")

            scraper = LinkedInScraper(config, config.get("time_filter", "r604800"), blacklist, playwright=p)
            mode: str = dbg.get("mode", "single_link")
            if mode == "single_link":
                job_links: List[str] = dbg.get("job_links", [])
                if job_links:
                    for job_link in job_links:
                        logger.info(f"Debug job link: {job_link}")
                        try:
                            job_id_match = re.search(r"/jobs/view/(\d+)/?", job_link)
                            if not job_id_match:
                                logger.error(f"Could not extract job ID from URL: {job_link}")
                                continue
                            job_id = job_id_match.group(1)
                        except Exception as e:
                            logger.error(f"Parse error: {e}")
                            continue
                        job_data = await scraper.get_job_details(page, job_id, conn)
                        if job_data:
                            logger.info("Debug extracted:\n" + json.dumps(job_data, indent=2))
                        else:
                            logger.error(f"Failed for {job_link}")
                elif len(args.extra) > 0:
                    job_link: str = args.extra[0]
                    logger.info(f"Debug provided job link: {job_link}")
                    try:
                        job_id_match = re.search(r"/jobs/view/(\d+)/?", job_link)
                        if not job_id_match:
                            logger.error(f"Could not extract job ID from URL: {job_link}")
                            return
                        job_id = job_id_match.group(1)
                    except Exception as e:
                        logger.error(f"Parse error: {e}")
                        return
                    job_data = await scraper.get_job_details(page, job_id, conn)
                    if job_data:
                        logger.info("Debug extracted:\n" + json.dumps(job_data, indent=2))
                    else:
                        logger.error("Failed to extract job details.")
                else:
                    logger.error("No job links provided for debug (single_link).")
            elif mode == "search_mode":
                query: Dict[str, Any] = squeries[0]
                kw: str = query["keywords"]
                loc: str = query["location"]
                remote: Optional[Any] = query.get("remote", None)
                logger.info(f"Debug search: kw={kw}, loc={loc}, remote={remote}")
                job_infos = await scraper.search_jobs(page, kw, loc, remote)
                limit: int = dbg.get("search_limit", 1)
                for info in job_infos[:limit]:
                    job_data = await scraper.get_job_details(page, info["job_id"], conn, search_card_info=info)
                    if job_data:
                        logger.info("Debug extracted:\n" + json.dumps(job_data, indent=2))
                    else:
                        logger.error(f"Failed for job id {info['job_id']}")
            elif mode == "rescrape_by_db_query":
                sql_query: Optional[str] = dbg.get("sql_query")
                if not sql_query:
                    logger.error("No SQL query provided for rescrape_by_db_query. Aborting.")
                    return
                cursor = conn.execute(sql_query)
                rows = cursor.fetchall()
                logger.info(f"Found {len(rows)} records to rescrape.")
                concurrent_limit: int = dbg.get("concurrent_details", 15)
                semaphore = asyncio.Semaphore(concurrent_limit)

                async def rescrape_job(row: Any) -> None:
                    async with semaphore:
                        job_url: str = row["job_url"] if isinstance(row, dict) else row[1]
                        try:
                            job_id_match = re.search(r"/jobs/view/(\d+)/?", job_url)
                            if not job_id_match:
                                logger.error(f"Could not extract job ID from URL: {job_url}")
                                return
                            job_id = job_id_match.group(1)
                        except Exception as e:
                            logger.error(f"Error parsing job_id from {job_url}: {e}")
                            return
                        logger.info(f"Rescraping job id {job_id} from {job_url}")
                        detail_page = await context.new_page()
                        await detail_page.route("**/*", block_resources)
                        job_data = await scraper.get_job_details(detail_page, job_id, conn)
                        await detail_page.close()
                        if job_data:
                            logger.info("Rescraped details:\n" + json.dumps(job_data, indent=2))
                            insert_job(conn, job_data)
                            conn.commit()
                        else:
                            logger.error(f"Failed to rescrape job id {job_id}")

                tasks = [rescrape_job(row) for row in rows]
                await asyncio.gather(*tasks)
            else:
                logger.error("Unrecognized debug mode. Use 'single_link', 'search_mode', or 'rescrape_by_db_query'.")

            await context.close()
            conn.close()
            await browser.close()
            return

        recipe: List[str] = args.recipe.split(",") if args.recipe else ["scrap", "process"]
        tasks: List[Any] = []
        if "scrap" in recipe:
            tasks.append(
                asyncio.create_task(
                    run_scrape(
                        config,
                        browser,
                        squeries,
                        args.manual_login,
                        endless=args.endless,
                        args=args,
                        playwright=p,
                        status=status,
                    )
                )
            )
        if "process" in recipe:
            tasks.append(
                asyncio.create_task(
                    process_and_rank_jobs(config, args.verbose, config.get("threads", 4))
                )
            )

        if tasks:
            if not args.endless:
                await asyncio.gather(*tasks)
                await asyncio.sleep(3)
                for t in tasks:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        logger.info("Task cancelled.")
            else:
                await asyncio.gather(*tasks)
            await browser.close()
    logger.info("Scraping & processing complete. Browser closed.")
    live_task.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Advanced LinkedIn Scraper", add_help=False
    )
    parser.add_argument("-h", "--hours", type=int, default=None, help="Time filter in hours")
    parser.add_argument("--manual-login", action="store_true", help="Manual login")
    parser.add_argument(
        "--recipe",
        type=str,
        default="scrap",
        help="Run mode: 'scrap', 'process', or 'scrap,process'",
    )
    parser.add_argument("--endless", action="store_true", help="Scrape continuously")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument("--debug-single", action="store_true", help="Scrape a single job and exit for debugging")
    parser.add_argument("extra", nargs="*", help="Optional job URL for debug-single mode")
    parser.add_argument("--help", action="help", help="Show help message")
    args = parser.parse_args()
    set_verbose(args.verbose)
    asyncio.run(main_scraper(args))