#!/usr/bin/env python3
"""
LinkedIn Scraper (Enhanced)

This script scrapes LinkedIn job listings using robust selectors, predictive pagination,
and concurrent detail-page extraction. It includes resource blocking, dynamic DOM handling,
and fallback logic to maximize data extraction even when the page structure changes.

It supports a debug mode to scrape a single job and exit.
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
    load_config, ensure_logged_in, simulate_human_behavior,
    get_company_size, get_company_size_score, block_resources
)
from jobfuq.database import (
    create_connection, create_table, create_blacklist_table, load_blacklist,
    insert_job, job_exists, is_company_blacklisted
)
from jobfuq.processor import process_and_rank_jobs

SPEED_QUANTIFIER: int = 1


# ------------------------------------------------------------------------------
# LinkedInScraper Class
# ------------------------------------------------------------------------------
class LinkedInScraper:
    """
    A class to scrape LinkedIn job listings with multiple fallback selectors,
    concurrent detail-page extraction, and blacklist checking.
    """

    def __init__(self, config: Dict[str, Any], time_filter: str, blacklist_data: Dict[str, Any]) -> None:
        """
        Initialize the scraper with configuration, time filter, and blacklist data.

        :param config: Configuration dictionary.
        :param time_filter: Time filter string (e.g., 'r604800').
        :param blacklist_data: Dictionary with blacklist and whitelist sets.
        """
        self.config: Dict[str, Any] = config
        self.time_filter: str = time_filter
        self.blacklist_data: Dict[str, Any] = blacklist_data
        self.base_url: str = "https://www.linkedin.com"
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
        search_url = f"{self.base_url}/jobs/search/?keywords={keywords}&location={location}&f_TPR={self.time_filter}"
        if remote is True:
            search_url += "&f_WT=2"
        elif remote is False:
            search_url += "&f_WT=1"
        logger.info(f"Navigating to: {search_url}")
        await page.goto(search_url, wait_until="domcontentloaded")
        logger.info(f"Landed on: {page.url}")
        # Uncomment next line to enable human behavior simulation
        # await simulate_human_behavior(page)
        logger.info("Waiting for job list container...")

        for sel in [".scaffold-layout__list", ".jobs-search-results"]:
            try:
                await page.wait_for_selector(sel, timeout=30000)
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
            new_infos: List[Dict[str, Any]] = await self.extract_job_infos(page)
            job_infos.extend(new_infos)
            # Remove duplicates by job_id
            job_infos = list({x["job_id"]: x for x in job_infos}.values())
            logger.info(f"Total job infos so far => {len(job_infos)}")
            if len(job_infos) >= max_postings:
                break
            old_count: int = len(job_infos)
            await page.evaluate("window.scrollBy(0, 5000)")
            await asyncio.sleep(random.uniform(2, 3) * SPEED_QUANTIFIER)
            new_infos = await self.extract_job_infos(page)
            job_infos.extend(new_infos)
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
        Extract minimal job info from job cards using several fallback selectors.

        :param page: The Playwright page instance.
        :return: A list of dictionaries with basic job data.
        """
        results: List[Dict[str, Any]] = []
        logger.info("Extracting job cards from current page...")
        # Use multiple selectors to capture job cards
        known_selectors: List[str] = [
            "li.scaffold-layout__list-item div.job-card-container",
            "li.jobs-search-results__list-item div.job-card-container",
            "div.job-card-list__container"
        ]

        job_cards: List[Any] = []
        for sel in known_selectors:
            try:
                job_cards = await page.query_selector_all(sel)
                if job_cards:
                    logger.info(f"Found {len(job_cards)} job cards using selector: {sel}")
                    break
            except Exception as e:
                logger.debug(f"Error finding job cards with {sel}: {e}")

        if not job_cards:
            logger.debug("No job cards found with known selectors.")
            return results

        blacklist: set = self.blacklist_data.get("blacklist", set())
        whitelist: set = self.blacklist_data.get("whitelist", set())
        for card in job_cards:
            # Try multiple selectors for the job title
            title_elem = await card.query_selector("a.job-card-list__title")
            if not title_elem:
                title_elem = await card.query_selector("div.full-width.artdeco-entity-lockup__title a span strong")
            title: str = (await title_elem.text_content()).strip() if title_elem else ""
            logger.info(f"Extracted job title: '{title}'")
            title_lower: str = title.lower()
            if title and any(bk in title_lower for bk in blacklist) and not any(wk in title_lower for wk in whitelist):
                logger.info(f"Skipped blacklisted title => '{title}'")
                continue

            job_id: Optional[str] = await self.extract_job_id(card)
            if not job_id:
                logger.warning(f"Skipping job due to missing job ID: '{title}'")
                continue

            company: str = await self.extract_company_name(card)
            loc: str = await self.extract_location(card)
            descr: str = await self.extract_brief_description(card)
            applicants_count: Optional[int] = await self.extract_applicants_count(card)
            csize: Optional[str] = await self.extract_company_size_from_card(card)

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
        Extract the job ID from a job card element using various attributes.

        :param card: The job card element.
        :return: The job ID string if found, otherwise None.
        """
        for attr in ["data-occludable-job-id", "data-job-id", "data-id"]:
            val = await card.get_attribute(attr)
            if val:
                return val
        return None

    async def extract_company_name(self, card: Any) -> str:
        """
        Extract the company name from a job card element using multiple selectors.

        :param card: The job card element.
        :return: The company name.
        """
        selectors: List[str] = [
            "h4.job-card-container__company-name",
            "span.job-card-container__company-name",
            "div.job-card-list__company-name",
            "h3.job-card-list__company-name"
        ]
        for sel in selectors:
            elem = await card.query_selector(sel)
            if elem:
                text = (await elem.text_content()).strip()
                if text:
                    return text
        return ""

    async def extract_location(self, card: Any) -> str:
        """
        Extract the job location from a job card element.

        :param card: The job card element.
        :return: The location text.
        """
        selectors: List[str] = [
            "li.job-card-container__metadata-item",
            "span.job-card-container__metadata-item",
            ".job-card-list__location"
        ]
        for sel in selectors:
            elem = await card.query_selector(sel)
            if elem:
                text = (await elem.text_content()).strip()
                if text:
                    return text
        return ""

    async def extract_brief_description(self, card: Any) -> str:
        """
        Extract a brief job description from a job card.

        :param card: The job card element.
        :return: The description text.
        """
        snippet_selectors: List[str] = [
            "p.job-card-list__snippet",
            ".job-card-container__snippet"
        ]
        for sel in snippet_selectors:
            elem = await card.query_selector(sel)
            if elem:
                text = (await elem.text_content()).strip()
                if text:
                    return text
        return ""

    async def extract_applicants_count(self, card: Any) -> Optional[int]:
        """
        Extract the number of applicants from a job card element.

        :param card: The job card element.
        :return: The applicants count as an integer, or None if not found.
        """
        elem = await card.query_selector("span.job-card-container__applicant-count")
        if not elem:
            return None
        text = (await elem.text_content()) or ""
        match = re.search(r'(\d+)', text)
        return int(match.group(1)) if match else None

    async def extract_company_size_from_card(self, card: Any) -> Optional[str]:
        """
        Extract the company size from a job card element.

        :param card: The job card element.
        :return: The company size string if found, otherwise None.
        """
        try:
            csize = await card.get_attribute("data-company-size")
            if csize:
                return csize.strip()
            size_elem = await card.query_selector("span.job-card-company-size")
            if size_elem:
                txt = await size_elem.text_content()
                if txt:
                    return txt.strip()
        except Exception as e:
            logger.debug(f"Company size from card error: {e}")
        return None

    async def go_to_next_page(self, page: Any, current_page: int) -> bool:
        """
        Attempt to navigate to the next page of job listings by clicking on pagination buttons.

        :param page: The Playwright page instance.
        :param current_page: The current page number.
        :return: True if pagination was successful, otherwise False.
        """
        next_num: int = current_page + 1
        logger.info(f"Attempting to go to next page => Page {next_num}")

        selectors: List[str] = [
            f"button[aria-label='Page {next_num}']",
            f"a[aria-label='Page {next_num}']",
            "button.artdeco-pagination__button--next",
            "button[aria-label='Next']",
            "a[aria-label='Next']"
        ]

        for sel in selectors:
            try:
                btn = await page.query_selector(sel)
                if btn:
                    disabled = (await btn.get_attribute("disabled")) or (await btn.get_attribute("aria-disabled"))
                    if disabled and disabled.lower() in ["true", "disabled"]:
                        logger.info(f"Pagination button '{sel}' is disabled.")
                        continue
                    logger.info(f"Clicking pagination => '{sel}'")
                    await btn.click()
                    await page.wait_for_load_state("domcontentloaded")
                    await asyncio.sleep(2)
                    return True
            except Exception as e:
                logger.debug(f"Error clicking pagination '{sel}': {e}")

        logger.info(f"No valid pagination found for page {next_num}, stopping.")
        return False

    async def get_field_content(self, page: Any, selectors: List[str], default: str = "") -> str:
        """
        Iterate over a list of selectors and return the text content from the first element found.

        :param page: The Playwright page instance.
        :param selectors: A list of CSS selectors.
        :param default: The default text if none found.
        :return: The extracted text content.
        """
        for sel in selectors:
            content = await self.get_text_content(page, sel, default="")
            if content:
                return content
        return default

    async def get_job_details(self, page: Any, job_id: str, search_card_info: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """
        Load the full job detail page and extract additional information with multiple fallback selectors.

        :param page: The Playwright page instance.
        :param job_id: The job's unique identifier.
        :param search_card_info: Fallback information from the search results.
        :return: A dictionary of job details, or None on failure.
        """
        jurl: str = f"{self.base_url}/jobs/view/{job_id}/"
        logger.info(f"Navigating to job detail => {jurl}")

        try:
            await page.goto(jurl, wait_until="domcontentloaded")
            # Uncomment the next line if you wish to simulate human behavior on the detail page.
            # await simulate_human_behavior(page)
        except PlaywrightTimeoutError:
            logger.error(f"Timeout loading detail => job {job_id}")
            return None
        except Exception as e:
            logger.error(f"Error loading detail => job {job_id}: {e}")
            return None

        try:
            # Start with fallback data from search card if available.
            title: str = search_card_info.get("title", "") if search_card_info else ""
            company: str = search_card_info.get("company", "") if search_card_info else ""
            loc: str = search_card_info.get("location", "") if search_card_info else ""
            descr: str = search_card_info.get("description", "") if search_card_info else ""

            if not title:
                title = await self.get_field_content(page, [
                    "h1.jobs-unified-top-card__job-title",
                    "h1.t-24.t-bold.inline",
                    "div.job-details-jobs-unified-top-card__sticky-header-job-title strong",
                    "h3.job-card-list__title"
                ], default="")
            if not company:
                company = await self.get_field_content(page, [
                    "a.jobs-unified-top-card__company-url",
                    "div.job-details-jobs-unified-top-card__company-name a",
                    "a[data-test-app-aware-link]"
                ], default="")
            if not loc:
                loc = await self.get_field_content(page, [
                    "span.jobs-unified-top-card__bullet",
                    "div.job-details-jobs-unified-top-card__primary-description-container span.tvm__text--low-emphasis",
                    "span.job-card-container__location"
                ], default="")
            if not descr:
                descr = await self.get_field_content(page, [
                    "div.jobs-description-content__text",
                    "section.description",
                    "article.jobs-description__container",
                    "div.jobs-box__html-content"
                ], default="")

            posted_t: str = await self.get_field_content(page, [
                "span.posted-time-ago__text",
                "time[datetime]",
                "span.tvm__text--positive"
            ], default="")
            date_str: str = self.parse_posting_date(posted_t)

            company_url: str = await self.get_field_content(page, [
                "a.jobs-unified-top-card__company-url",
                "div.job-details-jobs-unified-top-card__company-name a"
            ], default="")

            company_size: str = await self.get_field_content(page, [
                "span.jobs-company__inline-information",
                "div.org-top-card-summary-info-list__info-item"
            ], default="Unknown")

            applicants_text: str = await self.get_field_content(page, [
                "span.jobs-unified-top-card__applicant-count",
                "span.job-card-container__applicant-count"
            ], default="Unknown")
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
        Try to extract text content from an element using a given selector.

        :param page: The Playwright page instance.
        :param selector: CSS selector to locate the element.
        :param default: Default text to return if extraction fails.
        :return: The text content or default.
        """
        try:
            el = await page.wait_for_selector(selector, timeout=5000)
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

        :param posted_time: The raw posted time string.
        :return: The normalized date string.
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
        Remove HTML tags and excess whitespace from a text string.

        :param txt: The raw HTML text.
        :return: The cleaned text.
        """
        if not txt:
            return ""
        c = re.sub(r"<[^>]+>", "", txt)
        return re.sub(r"\s+", " ", c).strip()


async def evaluate_job(ai_model: AIModel, job: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluate a job by merging the original job data with AI evaluation results.

    :param ai_model: An instance of AIModel.
    :param job: A dictionary containing the original job data.
    :return: Combined job data and evaluation metrics.
    """
    evaluation: Dict[str, Any] = await ai_model.evaluate_job_fit(job)
    return {**job, **evaluation}


if __name__ == "__main__":
    # Command-line arguments parsing
    parser = argparse.ArgumentParser(
        description="Advanced LinkedIn Scraper: robust pagination, dynamic selectors, and concurrent job detail extraction.",
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

    asyncio.run(main_scraper(args))
