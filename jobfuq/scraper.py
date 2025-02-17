import sys, asyncio, argparse, json, random, re, time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from rich.console import Console
from jobfuq.logger import logger, set_verbose
from jobfuq.linked_utils import ensure_logged_in, simulate_human_behavior, get_company_size, get_company_size_score, block_resources
from jobfuq.utils import load_config
from jobfuq.database import create_connection, create_table, create_blacklist_table, load_blacklist, insert_job, job_exists, is_company_blacklisted
from jobfuq.processor import process_and_rank_jobs
from jobfuq.llm_handler import AIModel

console = Console()

def print_config_flags(config: Dict[str, Any], args: argparse.Namespace) -> None:
    flags = {
        "Manual Login": args.manual_login,
        "Debug Mode": args.debug_single or config.get("debug", {}).get("enabled", False),
        "Headless": config.get("headless", False),
        "Scraping Mode": config.get("scraping", {}).get("mode", "normal"),
        "Verbose": args.verbose or config.get("debug", {}).get("verbose", False)
    }
    output = []
    for key, value in flags.items():
        mark = "[green]✅[/green]" if value else "[red]❌[/red]"
        output.append(f"{key}: {value} {mark}")
    console.print("Configuration Flags:", style="bold cyan")
    console.print("\n".join(output), style="bold yellow")


class LinkedInScraper:
    def __init__(self, config: Dict[str, Any], time_filter: str, blacklist_data: Dict[str, Any]):
        self.config, self.time_filter, self.blacklist_data = config, time_filter, blacklist_data
        try:
            self.scraper_config = load_config("jobfuq/conf/linked_config.toml")
            logger.info("Loaded scraper config from linked_config.toml")
        except Exception as e:
            logger.error(f"Failed to load scraper config: {e}"); self.scraper_config = {}
        urls_conf, timeouts_conf, attrs_conf = self.scraper_config.get("urls", {}), self.scraper_config.get("timeouts", {}), self.scraper_config.get("attributes", {})
        self.base_url = urls_conf.get("base_url", "https://www.linkedin.com")
        self.wait_until = urls_conf.get("wait_until", "domcontentloaded")
        self.selector_timeout, self.text_timeout = timeouts_conf.get("selector_timeout", 30000), timeouts_conf.get("get_text_timeout", 5000)
        self.job_id_attrs = attrs_conf.get("job_id", ["data-occludable-job-id", "data-job-id", "data-id"])
        self.scraping_mode = config.get("scraping", {}).get("mode", "normal").lower()
        if self.scraping_mode == "aggressive":
            logger.info("Aggressive mode active: reducing text timeout")
            self.text_timeout = 1000
        self.company_size_cache: Dict[str, str] = {}

    async def search_jobs(self, page: Any, keywords: str, location: str, remote: Optional[bool] = None) -> List[Dict[str, Any]]:
        sp = self.scraper_config.get("urls", {}).get("search_url_pattern", "/jobs/search/?keywords={keywords}&location={location}&f_TPR={time_filter}")
        search_url = self.base_url + sp.format(keywords=keywords, location=location, time_filter=self.time_filter)
        if remote is True: search_url += "&f_WT=2"
        elif remote is False: search_url += "&f_WT=1"
        logger.info(f"Navigating to: {search_url}"); await page.goto(search_url, wait_until=self.wait_until); logger.info(f"Landed on: {page.url}")
        await simulate_human_behavior(page)
        for sel in self.scraper_config.get("jobs", {}).get("job_list_selectors", []):
            try:
                await page.wait_for_selector(sel, timeout=self.selector_timeout); logger.info(f"Job list found: {sel}"); break
            except PlaywrightTimeoutError: logger.debug(f"No job list with {sel}")
        job_infos, page_num, max_postings = [], 1, self.config.get("max_postings", 100)
        while len(job_infos) < max_postings:
            logger.info(f"Current page: {page.url}")
            job_infos.extend(await self.extract_job_infos(page)); job_infos = list({x["job_id"]: x for x in job_infos}.values())
            logger.info(f"Total jobs so far: {len(job_infos)}")
            if len(job_infos) >= max_postings: break
            old_count = len(job_infos); await page.evaluate("window.scrollBy(0, 5000)"); await asyncio.sleep(random.uniform(2, 3))
            job_infos.extend(await self.extract_job_infos(page)); job_infos = list({x["job_id"]: x for x in job_infos}.values())
            if len(job_infos) == old_count:
                logger.info("No new postings; attempting pagination.");
                if not await self.go_to_next_page(page, page_num):
                    logger.info("Pagination failed."); break
                if page.url == page.url: break; page_num += 1
        return job_infos[:max_postings]

    async def fetch_applicants_count(self, context_obj: Any) -> Optional[int]:
        au_conf = self.scraper_config.get("applicants_update", {}); selectors, xpaths, patterns = au_conf.get("selectors", []), au_conf.get("xpaths", []), au_conf.get("patterns", [])
        for sel in selectors:
            try:
                elem = await context_obj.query_selector(sel)
                if elem:
                    text = (await elem.text_content() or "").strip()
                    for pattern in patterns:
                        if (match := re.search(pattern, text, re.IGNORECASE)): return int(match.group(1))
            except Exception: continue
        for xpath in xpaths:
            try:
                elem = await context_obj.query_selector(f"xpath={xpath}")
                if elem:
                    text = (await elem.text_content() or "").strip()
                    for pattern in patterns:
                        if (match := re.search(pattern, text, re.IGNORECASE)): return int(match.group(1))
            except Exception: continue
        try:
            full_text = (await context_obj.text_content() or "").strip()
            for pattern in patterns:
                if (match := re.search(pattern, full_text, re.IGNORECASE)): return int(match.group(1))
        except Exception: pass
        try:
            page = context_obj.page if hasattr(context_obj, "page") else context_obj
            full_page_text = await page.evaluate("document.body.innerText")
            for pattern in patterns:
                if (match := re.search(pattern, full_page_text, re.IGNORECASE)): return int(match.group(1))
        except Exception: pass
        logger.warning("❌ No applicants count found."); return None

    async def extract_applicants_count(self, card: Any) -> Optional[int]:
        return await self.fetch_applicants_count(card)

    async def extract_job_infos(self, page: Any) -> List[Dict[str, Any]]:
        results = []; logger.info("Extracting job cards...")
        card_selectors = self.scraper_config.get("jobs", {}).get("job_card_selectors", [])
        job_cards = []
        for sel in card_selectors:
            try:
                if (found := await page.query_selector_all(sel)):
                    job_cards = found; logger.info(f"Found {len(found)} cards using: {sel}"); break
            except Exception as ex: logger.debug(f"Error with {sel}: {ex}")
        if not job_cards:
            logger.debug("No job cards found."); return results
        blacklist, whitelist = self.blacklist_data.get("blacklist", set()), self.blacklist_data.get("whitelist", set())
        c = self.scraper_config.get("card", {}); title_selectors = c.get("title_selectors", []); company_selectors = c.get("company_selectors", []); location_selectors = c.get("location_selectors", []); snippet_selectors = c.get("snippet_selectors", []); company_size_selectors = c.get("company_size_selectors", [])
        for card in job_cards:
            title_elem = None
            for sel in title_selectors:
                title_elem = await card.query_selector(sel)
                if title_elem: break
            title = (await title_elem.text_content()).strip() if title_elem else ""
            logger.info(f"Extracted title: '{title}'")
            if title and any(bk in title.lower() for bk in blacklist) and not any(wk in title.lower() for wk in whitelist):
                logger.info(f"Skipped blacklisted: '{title}'"); continue
            job_id = await self.extract_job_id(card)
            if not job_id:
                logger.warning(f"Missing job ID for: '{title}'"); continue
            company = "";
            for sel in company_selectors:
                if (elem := await card.query_selector(sel)) and (company := (await elem.text_content()).strip()): break
            loc = ""
            for sel in location_selectors:
                if (elem := await card.query_selector(sel)) and (loc := (await elem.text_content()).strip()): break
            descr = ""
            for sel in snippet_selectors:
                if (elem := await card.query_selector(sel)) and (descr := (await elem.text_content()).strip()): break
            applicants_count = await self.extract_applicants_count(card)
            csize = None
            for sel in company_size_selectors:
                if (elem := await card.query_selector(sel)) and (txt := await elem.text_content()):
                    csize = txt.strip(); break
            results.append({
                "job_id": job_id, "title": title, "company": company, "location": loc,
                "description": descr, "applicants_count": applicants_count, "company_size": csize or "Unknown"
            })
        logger.info(f"Extracted {len(results)} job cards.")
        return results

    async def extract_job_id(self, card: Any) -> Optional[str]:
        for attr in self.job_id_attrs:
            if (val := await card.get_attribute(attr)): return val
        return None

    async def go_to_next_page(self, page: Any, current_page: int) -> bool:
        next_num = current_page + 1; logger.info(f"Next page: {next_num}")
        for sel in self.scraper_config.get("pagination", {}).get("selectors", []):
            try:
                formatted_sel = sel.format(page=next_num)
            except Exception: formatted_sel = sel
            try:
                if (btn := await page.query_selector(formatted_sel)):
                    disabled = (await btn.get_attribute("disabled")) or (await btn.get_attribute("aria-disabled"))
                    if disabled and disabled.lower() in ["true", "disabled"]:
                        logger.info(f"Button '{formatted_sel}' disabled."); continue
                    logger.info(f"Clicking pagination: '{formatted_sel}'"); await btn.click(); await page.wait_for_load_state(self.wait_until); await asyncio.sleep(2); return True
            except Exception as e: logger.debug(f"Error with pagination '{formatted_sel}': {e}")
        logger.info(f"No valid pagination for page {next_num}."); return False

    async def get_field_content(self, page: Any, selectors: List[str], default: str = "") -> str:
        desc_text = ""
        for sel in selectors:
            content = await self.get_text_content(page, sel, default="")
            if content:
                if self.config.get("debug", {}).get("log_selectors", False):
                    logger.info(f"Selector {sel} found: {content[:500]}")
                if len(content) > len(desc_text): desc_text = content
        return desc_text if desc_text else default

    async def get_job_details(self, page: Any, job_id: str, search_card_info: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        jurl = f"{self.base_url}/jobs/view/{job_id}/"
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

        # NEW: Check if the job is no longer accepting applications
        closed_app_text = self.scraper_config.get("applicants_update", {}).get("closed_application_text", "no longer accepting applications").lower()
        feedback_elem = await page.query_selector(".artdeco-inline-feedback__message")
        if feedback_elem:
            feedback_text = (await feedback_elem.text_content() or "").strip().lower()
            if closed_app_text in feedback_text:
                logger.info(f"Job {job_id} is closed (no longer accepting applications). Skipping addition to DB.")
                return None

        if self.config.get("debug", {}).get("force_expand", False):
            try:
                if (btn := await page.query_selector("button.jobs-description__expand")):
                    await btn.click()
                    await asyncio.sleep(1)
                    logger.info("Clicked 'See more'")
            except Exception as e:
                logger.warning(f"'See more' click failed: {e}")

        try:
            detail = self.scraper_config.get("detail", {})
            title_selectors = detail.get("title_selectors", [])
            company_selectors = detail.get("company_selectors", [])
            location_selectors = detail.get("location_selectors", [])
            description_selectors = detail.get("description_selectors", [])
            posted_time_selectors = detail.get("posted_time_selectors", [])
            company_url_selectors = detail.get("company_url_selectors", [])
            company_size_selectors = detail.get("company_size_selectors", [])

            title = search_card_info["title"] if search_card_info else ""
            company = search_card_info["company"] if search_card_info else ""
            loc = search_card_info["location"] if search_card_info else ""
            descr = search_card_info["description"] if search_card_info else ""

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
            applicants_count = await self.fetch_applicants_count(page)
            remote_flag = any(r in descr.lower() for r in ["remote", "wfh", "work from home", "work-from-home"])

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
                "job_state": "ACTIVE"
            }
            logger.info(f"Extracted details: {job_data['title']} @ {job_data['company']}")
            return job_data
        except Exception as e:
            logger.error(f"Failed for job {job_id}: {e}")
            return None


    async def get_text_content(self, page: Any, selector: str, default: str = "") -> str:
        try:
            if (el := await page.wait_for_selector(selector, timeout=self.text_timeout)):
                if (txt := await el.text_content()):
                    return txt.strip()
        except PlaywrightTimeoutError: pass
        except Exception as e: logger.debug(f"get_text_content error: {e}")
        return default

    def parse_posting_date(self, posted_time: Optional[str]) -> str:
        if not posted_time: return datetime.now().strftime("%Y-%m-%d")
        pt = posted_time.lower()
        try:
            if "minute" in pt or "hour" in pt or "just now" in pt: return datetime.now().strftime("%Y-%m-%d")
            if "yesterday" in pt: return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            if "day" in pt: return (datetime.now() - timedelta(days=int(re.search(r"(\d+)", pt).group()))).strftime("%Y-%m-%d")
            if "week" in pt: return (datetime.now() - timedelta(weeks=int(re.search(r"(\d+)", pt).group()))).strftime("%Y-%m-%d")
        except Exception as e: logger.error(f"Error parsing date: {e}")
        return datetime.now().strftime("%Y-%m-%d")

    def clean_html(self, txt: str) -> str:
        if not txt: return ""
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", txt)).strip()

    async def update_existing_job(self, conn: Any, job_url: str, page: Any) -> Optional[Dict[str, Any]]:
        try:
            logger.info(f"Updating job: {job_url}")
            await page.goto(job_url, wait_until=self.wait_until, timeout=30000)
            await simulate_human_behavior(page)

            # NEW: Check for "closed" application feedback on update
            closed_app_text = self.scraper_config.get("applicants_update", {}).get("closed_application_text", "no longer accepting applications").lower()
            feedback_elem = await page.query_selector(".artdeco-inline-feedback__message")
            if feedback_elem:
                feedback_text = (await feedback_elem.text_content() or "").strip().lower()
                if closed_app_text in feedback_text:
                    logger.info("Job is no longer accepting applications. Marking as CLOSED.")
                    job_state = "CLOSED"
                    applicants_count = 999
                else:
                    job_state = "ACTIVE"
                    applicants_count = await self.fetch_applicants_count(page)
            else:
                job_state = "ACTIVE"
                applicants_count = await self.fetch_applicants_count(page)

            current_time = int(time.time() * 1000)
            conn.execute(
                "UPDATE job_listings SET applicants_count = ?, last_checked = ?, application_status = ?, job_state = ? WHERE job_url = ?",
                (applicants_count, current_time, "closed" if job_state == "CLOSED" else "not applied", job_state, job_url)
            )
            conn.commit()
            logger.info(f"Updated {job_url}: count={applicants_count}, state={job_state}")
            return {
                "job_url": job_url,
                "applicants_count": applicants_count,
                "job_state": job_state,
                "last_checked": current_time
            }
        except Exception as e:
            logger.error(f"Error updating {job_url}: {e}")
            return None


async def evaluate_job(ai_model: AIModel, job: Dict[str, Any]) -> Dict[str, Any]:
    evaluation: Dict[str, Any] = await ai_model.evaluate_job_fit(job); return {**job, **evaluation}

async def run_scrape(config: Dict[str, Any], browser: Any, search_queries: List[Dict[str, Any]], manual_login: bool, endless: bool = False, args: Optional[argparse.Namespace] = None) -> None:
    if endless:
        while True:
            async for job in get_jobcards(config, browser, search_queries, manual_login, args): pass
            logger.info("Round complete. Waiting 60 sec..."); await asyncio.sleep(60)
    else:
        async for job in get_jobcards(config, browser, search_queries, manual_login, args): pass

async def get_jobcards(config: Dict[str, Any], browser: Any, search_queries: List[Dict[str, Any]], manual_login: bool, args: Optional[argparse.Namespace]) -> Any:
    conn = create_connection(config); create_table(conn); create_blacklist_table(conn)
    try: blacklist = load_blacklist(conn)
    except Exception as e: logger.error(f"Error loading blacklist: {e}"); blacklist = {"blacklist": set(), "whitelist": set()}
    context = await browser.new_context(viewport={"width": 1280 + random.randint(-50, 50), "height": 720 + random.randint(-30, 30)}, user_agent=random.choice(config.get("user_agents", ["Mozilla/5.0"])))
    page = await context.new_page(); await page.route("**/*", block_resources)
    if manual_login:
        logger.info("Manual login selected. Log in & press Enter."); input("Press Enter after login..."); logger.info("Login done.")
    else:
        creds_pool = config.get("linkedin_credentials", {}).values()
        if not creds_pool:
            logger.error("No LinkedIn creds! Aborting."); return
        creds = random.choice(list(creds_pool)); username, password = creds["username"], creds["password"]
        logger.info(f"Auto-login with => {username}")
        if not await ensure_logged_in(page, username, password):
            logger.error("Auto-login failed. Aborting."); return
        logger.info("Logged in.")
    time_filter = config.get("time_filter", "r604800")
    scraper = LinkedInScraper(config, time_filter, blacklist)
    max_parallel = config.get("concurrent_details", 1); semaphore = asyncio.Semaphore(max_parallel)
    debug_job_url = args.extra[0] if args and args.debug_single and len(args.extra) > 0 else None
    if debug_job_url:
        logger.info(f"Debug: Using job URL => {debug_job_url}")
        async with semaphore:
            detail_page = await context.new_page(); await detail_page.route("**/*", block_resources)
            try:
                split_url = debug_job_url.rstrip("/").split("/"); job_id = split_url[-1] or split_url[-2]
                job_data = await scraper.get_job_details(detail_page, job_id)
            except Exception as e: logger.error(f"Parse error: {e}"); job_data = None
            await detail_page.close();
            if job_data: yield job_data
    else:
        for query in search_queries:
            kw, loc, remote = query["keywords"], query["location"], query.get("remote")
            logger.info(f"Scraping => kw={kw}, loc={loc}, remote={remote}")
            job_infos = await scraper.search_jobs(page, kw, loc, remote)
            tasks = []
            for info in job_infos:
                job_url = f"{scraper.base_url}/jobs/view/{info['job_id']}/"
                if job_exists(conn, job_url):
                    cursor = conn.execute("SELECT last_checked FROM job_listings WHERE job_url = ?", (job_url,))
                    row = cursor.fetchone(); current_time = int(time.time() * 1000)
                    if row is None or row[0] is None or (current_time - row[0] > 86400000):
                        logger.info(f"Job {job_url} outdated; updating...");
                        update_page = await context.new_page(); await update_page.route("**/*", block_resources)
                        updated_job = await scraper.update_existing_job(conn, job_url, update_page)
                        await update_page.close();
                        if updated_job: yield updated_job
                    else:
                        logger.debug(f"Job {job_url} exists and recent; skipping.")
                    continue
                tasks.append(asyncio.create_task(fetch_job_detail_task(scraper, info, conn, page.context, blacklist, semaphore)))
            if tasks:
                for detail in await asyncio.gather(*tasks, return_exceptions=True):
                    if isinstance(detail, dict) and detail: yield detail
    try:
        logger.info("Navigating to LinkedIn feed for cleanup."); await page.goto("https://www.linkedin.com/feed/", wait_until=scraper.wait_until); await simulate_human_behavior(page)
    except Exception: pass
    await context.close(); conn.close(); logger.info("Scraping complete. DB closed.")

async def fetch_job_detail_task(scraper: LinkedInScraper, info: Dict[str, Any], conn: Any, parent_context: Any, blacklist: Dict[str, set], sem: asyncio.Semaphore) -> Optional[Dict[str, Any]]:
    async with sem:
        page = await parent_context.new_page(); await page.route("**/*", block_resources)
        try:
            job_id = info["job_id"]; job_data = await scraper.get_job_details(page, job_id, search_card_info=info)
            if not job_data: return None
            if any(bk in job_data["title"].lower() for bk in blacklist["blacklist"]) and not any(wk in job_data["title"].lower() for wk in blacklist["whitelist"]):
                logger.info(f"Skipping blacklisted: {job_data['title']}"); return None
            if (job_data.get("company_url", "").lower() in [x.lower() for x in blacklist["blacklist"]] or
                    job_data["company"].lower() in [x.lower() for x in blacklist["blacklist"]] or
                    job_data["job_url"].lower() in [x.lower() for x in blacklist["blacklist"]] or
                    is_company_blacklisted(conn, job_data["company"], job_data.get("company_url", ""))):
                logger.info(f"Skipping blacklisted: {job_data['title']} at {job_data['company']}"); return None
            job_data["company_size_score"] = get_company_size_score(job_data["company_size"])
            logger.info(f"Saving job {job_data['job_id']} with desc: {job_data['description'][:500]}")
            insert_job(conn, job_data); conn.commit(); logger.info(f"Inserted job: {job_data['title']} @ {job_data['company']}"); return job_data
        except Exception as e: logger.error(f"Detail task error: {e}"); return None
        finally: await page.close()

async def main_scraper(args: argparse.Namespace) -> None:
    config = load_config("jobfuq/conf/config.toml")

    if args.hours is not None:
        config["time_filter"] = f"r{args.hours * 3600}"
        logger.info(f"Time filter: {config['time_filter']}")
    else:
        config.setdefault("time_filter", "r604800")

    config["manual_login"] = args.manual_login
    squeries = config.get("search_queries", [{"keywords": "Master of Internet Surfing", "location": "Netherlands", "remote": None}])
    headless_mode = config.get("headless", False)

    logger.info(f"Launching browser with headless={headless_mode}")
    print_config_flags(config, args)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless_mode, slow_mo=50)

        # --- DEBUG MODE HANDLING ---
        if args.debug_single or config.get("debug", {}).get("enabled", False):
            logger.info("Debug mode enabled.")
            dbg = config.get("debug", {})
            conn = create_connection(config)
            create_table(conn)
            create_blacklist_table(conn)

            try:
                blacklist = load_blacklist(conn)
            except Exception as e:
                logger.error(f"Blacklist error: {e}")
                blacklist = {"blacklist": set(), "whitelist": set()}

            context = await browser.new_context(
                viewport={"width": 1280 + random.randint(-50, 50), "height": 720 + random.randint(-30, 30)},
                user_agent=random.choice(config.get("user_agents", ["Mozilla/5.0"]))
            )

            page = await context.new_page()
            await page.route("**/*", block_resources)
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
                if not await ensure_logged_in(page, username, password):
                    logger.error("Auto-login failed. Aborting debug.")
                    return
                logger.info("Logged in.")

            scraper = LinkedInScraper(config, config.get("time_filter", "r604800"), blacklist)
            mode = dbg.get("mode", "single_link")

            if mode == "single_link":
                # Process provided job links
                job_links = dbg.get("job_links", [])
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

                        job_data = await scraper.get_job_details(page, job_id)
                        if job_data:
                            logger.info("Debug extracted:\n" + json.dumps(job_data, indent=2))
                        else:
                            logger.error(f"Failed for {job_link}")

                elif len(args.extra) > 0:
                    job_link = args.extra[0]
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

                    job_data = await scraper.get_job_details(page, job_id)
                    if job_data:
                        logger.info("Debug extracted:\n" + json.dumps(job_data, indent=2))
                    else:
                        logger.error("Failed to extract job details.")
                else:
                    logger.error("No job links provided for debug (single_link).")

            elif mode == "search_mode":
                # Use search with a limit
                query = squeries[0]
                kw, loc, remote = query["keywords"], query["location"], query.get("remote", None)
                logger.info(f"Debug search: kw={kw}, loc={loc}, remote={remote}")

                job_infos = await scraper.search_jobs(page, kw, loc, remote)
                for info in job_infos[: dbg.get("search_limit", 1)]:
                    job_data = await scraper.get_job_details(page, info["job_id"], search_card_info=info)
                    if job_data:
                        logger.info("Debug extracted:\n" + json.dumps(job_data, indent=2))
                    else:
                        logger.error(f"Failed for job id {info['job_id']}")

            elif mode == "rescrape_by_db_query":
                sql_query = dbg.get("sql_query")
                if not sql_query:
                    logger.error("No SQL query provided for rescrape_by_db_query. Aborting.")
                    return

                cursor = conn.execute(sql_query)
                rows = cursor.fetchall()
                logger.info(f"Found {len(rows)} records to rescrape.")

                concurrent_limit = dbg.get("concurrent_details", 15)  # Default to 2 if not set
                semaphore = asyncio.Semaphore(concurrent_limit)

                async def rescrape_job(row):
                    async with semaphore:
                        job_url = row["job_url"] if isinstance(row, dict) else row[1]
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
                        # Create a new page for each rescrape task
                        detail_page = await context.new_page()
                        await detail_page.route("**/*", block_resources)
                        job_data = await scraper.get_job_details(detail_page, job_id)
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

        # --- NORMAL MODE ---
        recipe = args.recipe.split(",") if args.recipe else ["scrap", "process"]
        tasks = []

        if "scrap" in recipe:
            tasks.append(asyncio.create_task(run_scrape(config, browser, squeries, args.manual_login, endless=args.endless, args=args)))

        if "process" in recipe:
            tasks.append(asyncio.create_task(process_and_rank_jobs(config, args.verbose, config.get("threads", 4))))

        if tasks:
            if not args.endless:
                await asyncio.gather(*tasks)
                await asyncio.sleep(10)
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

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Advanced LinkedIn Scraper", add_help=False)
    parser.add_argument("-h", "--hours", type=int, default=None, help="Time filter in hours")
    parser.add_argument("--manual-login", action="store_true", help="Manual login")
    parser.add_argument("--recipe", type=str, default="scrap", help="Run mode: 'scrap', 'process', or 'scrap,process'")
    parser.add_argument("--endless", action="store_true", help="Scrape continuously")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument("--debug-single", action="store_true", help="Scrape a single job and exit for debugging")
    parser.add_argument("extra", nargs="*", help="Optional job URL for debug-single mode")
    parser.add_argument("--help", action="help", help="Show help message")
    args = parser.parse_args(); set_verbose(args.verbose)
    asyncio.run(main_scraper(args))