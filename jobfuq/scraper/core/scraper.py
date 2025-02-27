import asyncio, random, re, time
from datetime import datetime, timedelta
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from jobfuq.logger.logger import logger
from jobfuq.scraper.core.linked_utils import linked_config, simulate_human_behavior
from jobfuq.utils.utils import load_config
from tenacity import retry, stop_after_attempt, wait_exponential
from typing import Any, Dict, List, Optional

class LinkedInScraper:
    def __init__(self, config, time_filter, blacklist_data, playwright=None):
        self.config = config
        self.time_filter = time_filter
        self.blacklist_data = blacklist_data
        self.playwright = playwright
        try:
            self.scraper_config = load_config('jobfuq/conf/linked_config.toml')
            logger.info('Loaded scraper config from linked_config.toml')
        except Exception as e:
            logger.error(f"Failed to load scraper config: {e}")
            self.scraper_config = {}

        urls_conf = self.scraper_config.get('urls', {})
        timeouts_conf = self.scraper_config.get('timeouts', {})
        attrs_conf = self.scraper_config.get('attributes', {})

        self.base_url = urls_conf.get('base_url', 'https://www.linkedin.com')
        self.wait_until = urls_conf.get('wait_until', 'domcontentloaded')
        self.selector_timeout = timeouts_conf.get('selector_timeout', 930000)
        self.text_timeout = timeouts_conf.get('get_text_timeout', 25000)

        self.job_id_attrs = attrs_conf.get('job_id', ['data-occludable-job-id', 'data-job-id', 'data-id'])
        self.scraping_mode = config.get('scraping', {}).get('mode', 'normal').lower()
        if self.scraping_mode == 'aggressive':
            logger.info('Aggressive mode active: reducing text timeout')
            self.text_timeout = 1000

        self.company_size_cache = {}

        # Setup local retry settings
        self.smart_retry_enabled = config.get('smart_retry_enabled', False)
        self.smart_retry_attempts = timeouts_conf.get('smart_retry_attempts', 2)
        self.smart_retry_sleep_min = timeouts_conf.get('smart_retry_sleep_min', 1)
        self.smart_retry_sleep_max = timeouts_conf.get('smart_retry_sleep_max', 3)

    async def robust_goto(self, page, url):
        """Navigate to the URL with retries if enabled."""
        attempts = self.smart_retry_attempts if self.smart_retry_enabled else 1
        for i in range(attempts):
            try:
                logger.info(f"Navigating to: {url} (attempt {i+1})")
                await page.goto(url, wait_until=self.wait_until, timeout=self.selector_timeout)
                return True
            except PlaywrightTimeoutError as e:
                logger.warning(f"Timeout navigating to {url} (attempt {i+1} of {attempts}): {e}")
                if i < attempts - 1:
                    sleep_dur = random.uniform(self.smart_retry_sleep_min, self.smart_retry_sleep_max)
                    logger.info(f"Retrying after {sleep_dur:.2f}s...")
                    await asyncio.sleep(sleep_dur)
                else:
                    logger.error(f"Exhausted retries for {url}")
                    return False
            except Exception as ex:
                logger.error(f"Error navigating to {url}: {ex}")
                return False

    async def search_jobs(self, page, keywords, location, remote=None):
        sp = self.scraper_config.get('urls', {}).get(
            'search_url_pattern',
            '/jobs/search/?keywords={keywords}&location={location}&f_TPR={time_filter}'
        )
        if remote is not None:
            sp += "&f_WT={remote}"
        search_url = self.base_url + sp.format(
            keywords=keywords,
            location=location,
            time_filter=self.time_filter,
            remote=remote
        )
        if not await self.robust_goto(page, search_url):
            logger.error(f"search_jobs failed to load {search_url}")
            return []
        # If a captcha/checkpoint appears, handle it via linked_utils
        from jobfuq.scraper.core.linked_utils import handle_manual_captcha
        if "checkpoint/challenge" in page.url:
            await handle_manual_captcha(page, self.playwright, self.config)
        logger.info(f"Landed on: {page.url}")
        from jobfuq.scraper.core.linked_utils import simulate_human_behavior
        await simulate_human_behavior(page)
        for sel in self.scraper_config.get('jobs', {}).get('job_list_selectors', []):
            try:
                await page.wait_for_selector(sel, timeout=self.selector_timeout)
                logger.info(f"Job list found: {sel}")
                break
            except PlaywrightTimeoutError:
                logger.debug(f"No job list with {sel}")
        job_infos = []
        page_num = 1
        max_postings = self.config.get('max_postings', 100)
        while len(job_infos) < max_postings:
            logger.info(f"Current page: {page.url}")
            job_infos.extend(await self.extract_job_infos(page))
            job_infos = list({x['job_id']: x for x in job_infos}.values())
            logger.info(f"Total jobs so far: {len(job_infos)}")
            if len(job_infos) >= max_postings:
                break
            old_count = len(job_infos)
            await page.evaluate('window.scrollBy(0, 5000)')
            await asyncio.sleep(random.uniform(2, 3))
            job_infos.extend(await self.extract_job_infos(page))
            job_infos = list({x['job_id']: x for x in job_infos}.values())
            if len(job_infos) == old_count:
                logger.info('No new postings; attempting pagination.')
                old_url = page.url
                if not await self.go_to_next_page(page, page_num):
                    logger.info('Pagination failed / no more pages.')
                    break
                if page.url == old_url:
                    break
                page_num += 1
        return job_infos[:max_postings]

    async def go_to_next_page(self, page, current_page):
        next_num = current_page + 1
        logger.info(f"Next page: {next_num}")
        for sel in self.scraper_config.get('pagination', {}).get('selectors', []):
            try:
                try:
                    formatted_sel = sel.format(page=next_num)
                except Exception:
                    formatted_sel = sel
                btn = await page.query_selector(formatted_sel)
                if btn:
                    disabled = await btn.get_attribute('disabled') or await btn.get_attribute('aria-disabled')
                    if disabled and disabled.lower() in ['true', 'disabled']:
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

    async def extract_job_infos(self, page):
        results = []
        logger.info('Extracting job cards...')
        card_selectors = self.scraper_config.get('jobs', {}).get('job_card_selectors', [])
        job_cards = []
        for sel in card_selectors:
            try:
                found = await page.query_selector_all(sel)
                if found:
                    job_cards = found
                    logger.info(f"Found {len(found)} cards using: {sel}")
                    break
            except Exception as ex:
                logger.debug(f"Error with {sel}: {ex}")
        if not job_cards:
            logger.debug('No job cards found.')
            return results
        card_conf = self.scraper_config.get('card', {})
        title_selectors = card_conf.get('title_selectors', [])
        company_selectors = card_conf.get('company_selectors', [])
        location_selectors = card_conf.get('location_selectors', [])
        snippet_selectors = card_conf.get('snippet_selectors', [])
        company_size_selectors = card_conf.get('company_size_selectors', [])
        for card in job_cards:
            title_elem = None
            for sel in title_selectors:
                title_elem = await card.query_selector(sel)
                if title_elem:
                    break
            title = (await title_elem.text_content()).strip() if title_elem else ''
            descr = ""
            for sel in snippet_selectors:
                elem = await card.query_selector(sel)
                if elem:
                    descr = (await elem.text_content()).strip()
                    if descr:
                        break
            from jobfuq.scraper.core.filter import passes_filter
            if not passes_filter(title, descr):
                logger.info(f"Job filtered out by whitelist rules: '{title}'")
                continue
            job_id = await self.extract_job_id(card)
            if not job_id:
                logger.warning(f"Missing job ID for: '{title}'")
                continue
            company = ''
            for sel in company_selectors:
                elem = await card.query_selector(sel)
                if elem:
                    company = (await elem.text_content()).strip()
                    if company:
                        break
            loc = ''
            for sel in location_selectors:
                elem = await card.query_selector(sel)
                if elem:
                    loc = (await elem.text_content()).strip()
                    if loc:
                        break
            applicants_count = await self.extract_applicants_count(card)
            csize = None
            for sel in company_size_selectors:
                elem = await card.query_selector(sel)
                if elem:
                    txt = await elem.text_content()
                    if txt:
                        csize = txt.strip()
                        break
            results.append({
                'job_id': job_id,
                'title': title,
                'company': company,
                'location': loc,
                'description': descr,
                'applicants_count': applicants_count,
                'company_size': csize or 'Unknown'
            })
        logger.info(f"Extracted {len(results)} job cards.")
        return results

    async def extract_job_id(self, card):
        for attr in self.job_id_attrs:
            val = await card.get_attribute(attr)
            if val:
                return val

    async def extract_applicants_count(self, card):
        return await self.fetch_applicants_count(card)

    async def fetch_applicants_count(self, context_obj):
        au_conf = self.scraper_config.get('applicants_update', {})
        selectors = au_conf.get('selectors', [])
        xpaths = au_conf.get('xpaths', [])
        patterns = au_conf.get('patterns', [])
        for sel in selectors:
            try:
                elem = await context_obj.query_selector(sel)
                if elem:
                    text = (await elem.text_content() or '').strip()
                    for pattern in patterns:
                        match = re.search(pattern, text, re.IGNORECASE)
                        if match:
                            return int(match.group(1))
            except Exception:
                continue
        for xpath in xpaths:
            try:
                elem = await context_obj.query_selector(f"xpath={xpath}")
                if elem:
                    text = (await elem.text_content() or '').strip()
                    for pattern in patterns:
                        match = re.search(pattern, text, re.IGNORECASE)
                        if match:
                            return int(match.group(1))
            except Exception:
                continue
        try:
            full_text = (await context_obj.text_content() or '').strip()
            for pattern in patterns:
                match = re.search(pattern, full_text, re.IGNORECASE)
                if match:
                    return int(match.group(1))
        except Exception:
            pass
        try:
            page = getattr(context_obj, 'page', None) or context_obj
            full_page_text = await page.evaluate('document.body.innerText')
            for pattern in patterns:
                match = re.search(pattern, full_page_text, re.IGNORECASE)
                if match:
                    return int(match.group(1))
        except Exception:
            pass
        logger.warning('❌ No applicants count found.')
        return None

    async def get_job_details(self, page, job_id, conn=None, **kwargs):
        jurl = f"{self.base_url}/jobs/view/{job_id}/"
        logger.info(f"Job detail: {jurl}")
        loaded = await self.robust_goto(page, jurl)
        if not loaded:
            logger.error(f"get_job_details: couldn't load {jurl}")
            return
        from jobfuq.scraper.core.linked_utils import simulate_human_behavior
        await simulate_human_behavior(page)
        closed_app_text = self.scraper_config.get('applicants_update', {}).get(
            'closed_application_text', 'no longer accepting applications'
        ).lower()
        feedback_elem = await page.query_selector('.artdeco-inline-feedback__message')
        if feedback_elem:
            feedback_text = (await feedback_elem.text_content() or '').strip().lower()
            if closed_app_text in feedback_text:
                logger.info(f"Job {job_id} is closed (no longer accepting applications).")
                if conn:
                    current_time = int(time.time() * 1000)
                    conn.execute(
                        'UPDATE job_listings SET applicants_count = ?, last_checked = ?, application_status = ?, job_state = ? WHERE job_url = ?',
                        (999, current_time, 'closed', 'CLOSED', jurl)
                    )
                    conn.commit()
                return
        detail_conf = self.scraper_config.get('detail', {})
        title_selectors = detail_conf.get('title_selectors', [])
        company_selectors = detail_conf.get('company_selectors', [])
        location_selectors = detail_conf.get('location_selectors', [])
        description_selectors = detail_conf.get('description_selectors', [])
        company_size_selectors = detail_conf.get('company_size_selectors', [])
        title = await self.get_field_content(page, title_selectors, default='')
        company = await self.get_field_content(page, company_selectors, default='')
        loc = await self.get_field_content(page, location_selectors, default='')
        descr = await self.get_field_content(page, description_selectors, default='')
        applicants_count = await self.fetch_applicants_count(page)
        company_size = await self.get_field_content(page, company_size_selectors, default='Unknown')
        job_data = {
            'job_id': job_id,
            'title': title.strip(),
            'company': company.strip(),
            'company_url': '',
            'location': loc.strip(),
            'description': self.clean_html(descr.strip()),
            'remote_allowed': False,
            'job_state': 'ACTIVE',
            'company_size': company_size,
            'company_size_score': 0,
            'job_url': jurl,
            'date': datetime.now().strftime('%Y-%m-%d'),
            'listed_at': int(time.time() * 1000),
            'applicants_count': applicants_count,
            'overall_relevance': 0.0,
            'is_posted': 1,
            'application_status': 'not applied'
        }
        logger.info(f"Extracted details: {job_data['title']} @ {job_data['company']}")
        return job_data

    async def get_field_content(self, page, selectors, default=''):
        desc_text = ''
        for sel in selectors:
            content = await self.get_text_content(page, sel, default='')
            if content and len(content) > len(desc_text):
                desc_text = content
        return desc_text if desc_text else default

    async def get_text_content(self, page, selector, default=''):
        try:
            el = await page.wait_for_selector(selector, timeout=self.text_timeout)
            if el:
                txt = await el.text_content()
                if txt:
                    return txt.strip()
        except PlaywrightTimeoutError:
            pass
        except Exception as e:
            logger.debug(f"get_text_content error: {e}")
        return default

    def parse_posting_date(self, posted_time):
        if not posted_time:
            return datetime.now().strftime('%Y-%m-%d')
        pt = posted_time.lower()
        try:
            if 'minute' in pt or 'hour' in pt or 'just now' in pt:
                return datetime.now().strftime('%Y-%m-%d')
            if 'yesterday' in pt:
                return (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
            if 'day' in pt:
                days = int(re.search('(\\d+)', pt).group())
                return (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            if 'week' in pt:
                weeks = int(re.search('(\\d+)', pt).group())
                return (datetime.now() - timedelta(weeks=weeks)).strftime('%Y-%m-%d')
        except Exception as e:
            logger.error(f"Error parsing date: {e}")
        return datetime.now().strftime('%Y-%m-%d')

    def clean_html(self, txt):
        if not txt:
            return ''
        return re.sub('\\s+', ' ', re.sub('<[^>]+>', '', txt)).strip()

    async def update_existing_job(self, conn, job_url, page):
        try:
            logger.info(f"Updating job: {job_url}")
            loaded = await self.robust_goto(page, job_url)
            if not loaded:
                logger.error(f"update_existing_job: couldn't load {job_url}")
                return
            from jobfuq.scraper.core.linked_utils import simulate_human_behavior
            await simulate_human_behavior(page)
            closed_app_text = self.scraper_config.get('applicants_update', {}).get(
                'closed_application_text', 'no longer accepting applications'
            ).lower()
            feedback_elem = await page.query_selector('.artdeco-inline-feedback__message')
            if feedback_elem:
                feedback_text = (await feedback_elem.text_content() or '').strip().lower()
                if closed_app_text in feedback_text:
                    logger.info(f"Job {job_url} is closed. Marking in DB as CLOSED.")
                    current_time = int(time.time() * 1000)
                    conn.execute(
                        'UPDATE job_listings SET applicants_count = ?, last_checked = ?, application_status = ?, job_state = ? WHERE job_url = ?',
                        (999, current_time, 'closed', 'CLOSED', job_url)
                    )
                    conn.commit()
                    return {
                        'job_url': job_url,
                        'applicants_count': 999,
                        'job_state': 'CLOSED',
                        'last_checked': current_time
                    }
            applicants_count = await self.fetch_applicants_count(page)
            current_time = int(time.time() * 1000)
            conn.execute(
                'UPDATE job_listings SET applicants_count = ?, last_checked = ?, job_state = ? WHERE job_url = ?',
                (applicants_count, current_time, 'ACTIVE', job_url)
            )
            conn.commit()
            logger.info(f"Updated {job_url}: count={applicants_count}, state=ACTIVE")
            return {
                'job_url': job_url,
                'applicants_count': applicants_count,
                'job_state': 'ACTIVE',
                'last_checked': current_time
            }
        except Exception as e:
            logger.error(f"Error updating {job_url}: {e}")
            return


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def get_company_size(page: Any, url: str) -> str:
    try:
        await page.goto(
            url,
            wait_until=linked_config.get("urls", {}).get("wait_until", "domcontentloaded"),
            timeout=730000,
        )
        await page.wait_for_load_state("domcontentloaded")
        await simulate_human_behavior(page)
        not_found_selector: str = linked_config.get("company", {}).get(
            "not_found_selector", 'h1:has-text("Page not found")'
        )
        if await page.query_selector(not_found_selector):
            logger.warning(f"Company page not found: {url}")
            return "Unknown"
        size_selectors: List[str] = linked_config.get("company", {}).get(
            "size_selectors",
            [
                ".t-normal.t-black--light.link-without-visited-state.link-without-hover-state",
                ".org-top-card-summary-info-list__info-item",
            ],
        )
        for selector in size_selectors:
            elements = await page.query_selector_all(selector)
            for element in elements:
                size_text: str = (await element.inner_text() or "").strip()
                if size_text:
                    return parse_company_size(size_text)
        summary_selector: str = ".org-top-card-summary-info-list"
        summary_element = await page.query_selector(summary_selector)
        if summary_element:
            summary_text: str = (await summary_element.inner_text()).strip()
            size_match = re.search(r"(\d{1,3}[Kk]?\+?)\s+employees?", summary_text)
            if size_match:
                return parse_company_size(size_match.group(1))
        logger.warning(f"Employee count not found on page: {url}")
        return "Unknown"
    except Exception as e:
        logger.error(f"Error scraping company size from {url}: {str(e)}")
        return "Unknown"


def parse_company_size(size_text: str) -> str:
    size_text = size_text.lower().strip()
    size_text = re.sub(r"(\d+)[,](\d+)", r"\1\2", size_text)
    size_text = re.sub(r"(\d+)\s*-\s*(\d+)", r"\1-\2", size_text)
    size_text = size_text.replace("k", "000").replace("K", "000").replace("+", "+")
    return size_text


def get_company_size_score(size_text: str) -> int:
    size_text = parse_company_size(size_text)
    if "unknown" in size_text:
        return 0
    size_map: Dict[str, int] = linked_config.get("size_map", {})
    for key, score in size_map.items():
        if key in size_text:
            return score
    return 0
