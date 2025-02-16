"""
Linked Utilities Module

This module contains helper functions for the LinkedIn scraper, including advanced
stealth techniques, human behavior simulation, CAPTCHA handling, session rotation,
and various utility routines.
"""

import json
import random
import asyncio
import re
import os
import time
from typing import Any, Dict, List, Optional

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, Route, Request
from playwright._impl._browser_type import BrowserType
from faker import Faker
from tenacity import retry, stop_after_attempt, wait_exponential

from jobfuq.logger import logger
from jobfuq.utils import load_config

# Directory to store session files
SESSION_STORE_DIR: str = 'session_store'
if not os.path.exists(SESSION_STORE_DIR):
    os.makedirs(SESSION_STORE_DIR, exist_ok=True)

config = load_config("jobfuq/conf/config.toml")


###############################
# Advanced Stealth Browser Setup
###############################
async def create_stealth_browser(browser_type: BrowserType) -> Any:
    """
    Launch a browser with randomized stealth options.

    Uses Faker to generate a random user agent, random window size, and a random timezone
    to help mask automated behavior.

    :param browser_type: The Playwright browser type to launch.
    :return: A launched browser instance.
    """
    fake = Faker()
    return await browser_type.launch(
        headless=False,  # Use headed mode for more natural behavior.
        args=[
            f'--user-agent={fake.user_agent()}',
            '--disable-blink-features=AutomationControlled',
            '--disable-web-security',
            '--disable-features=IsolateOrigins,site-per-process',
            f'--window-size={random.randint(1000,1600)},{random.randint(800,1200)}'
        ],
        env={
            'TZ': random.choice(['America/New_York', 'Europe/Paris', 'Asia/Singapore'])
        }
    )


###############################
# Anti-Fingerprinting Enhancements
###############################
async def apply_stealth_scripts(page: Any) -> None:
    """
    Inject scripts to mask browser fingerprinting.

    Prevents WebRTC leaks, spoofs canvas fingerprinting (only allowing 2d contexts),
    and disables the AudioContext.

    :param page: The Playwright page instance.
    """
    # Prevent WebRTC leaks
    await page.add_init_script("() => { window.RTCPeerConnection = undefined; }")
    # Canvas fingerprint spoofing: return null for non-'2d' contexts.
    await page.add_init_script("""
        () => {
            const getContext = HTMLCanvasElement.prototype.getContext;
            HTMLCanvasElement.prototype.getContext = function(type) {
                if (type === '2d') {
                    return getContext.call(this, type);
                }
                return null;
            }
        }
    """)
    # AudioContext masking
    await page.add_init_script("() => { window.AudioContext = undefined; }")


###############################
# Resource Blocking
###############################
async def block_resources(route: Route, request: Request) -> None:
    """
    Blocks specific resource types from loading to improve scraping performance.

    Blocks images, fonts, and media requests. Other resource types are allowed to load.

    :param route: The Playwright route object.
    :param request: The Playwright request object.
    """
    if request.resource_type in ["image", "font", "media"]:
        await route.abort()
    else:
        await route.continue_()


###############################
# Enhanced Traffic Pattern Masking
###############################
async def random_network_throttling(page: Any) -> None:
    """
    Simulate varied network conditions and random geolocation.

    Although Playwright doesn't natively support network condition emulation,
    this function sets a random geolocation and logs a random network profile.

    :param page: The Playwright page instance.
    """
    profiles = ['Wi-Fi', 'Regular4G', 'DSL']
    logger.debug(f"Applying random network profile: {random.choice(profiles)}")
    await page.context.set_geolocation({
        'latitude': random.uniform(-90, 90),
        'longitude': random.uniform(-180, 180)
    })


async def fake_http_traffic(page: Any) -> None:
    """
    Simulate additional benign HTTP traffic.

    Performs a harmless GET request to mimic normal background activity.

    :param page: The Playwright page instance.
    """
    try:
        async with page.context.request.new_context() as req_context:
            await req_context.get("https://httpbin.org/get")
    except Exception as e:
        logger.debug(f"Fake HTTP traffic error: {e}")


async def generate_realistic_mouse_physics(page: Any) -> None:
    """
    Simulate physics-based mouse movements.

    Generates realistic mouse trajectories between random start and end points.

    :param page: The Playwright page instance.
    """
    for _ in range(random.randint(3, 7)):
        start_x = random.randint(0, 1200)
        start_y = random.randint(0, 800)
        end_x = random.randint(0, 1200)
        end_y = random.randint(0, 800)
        steps = random.randint(120, 250)
        gravity = random.uniform(0.5, 1.5)
        wind = random.uniform(-0.5, 0.5)
        for i in range(steps):
            t = i / steps
            current_x = start_x + (end_x - start_x) * t
            current_y = start_y + (end_y - start_y) * t + gravity * t * wind
            await page.mouse.move(current_x, current_y)
            await asyncio.sleep(random.uniform(0.001, 0.005))


async def simulate_reading_patterns(page: Any) -> None:
    """
    Simulate slow scrolling to mimic a human reading a page.

    :param page: The Playwright page instance.
    """
    for _ in range(random.randint(1, 3)):
        scroll_distance = random.randint(200, 600)
        await page.mouse.wheel(0, scroll_distance)
        await asyncio.sleep(random.uniform(1, 2))


###############################
# Combined Human Behavior Simulation
###############################
async def simulate_human_behavior(page: Any) -> None:
    """
    Simulate a range of human-like interactions to evade bot detection.

    Combines scrolling, dynamic viewport resizing, network throttling,
    fake HTTP traffic, realistic mouse physics, reading patterns, and additional mouse movements.

    :param page: The Playwright page instance.
    """
    await scroll_randomly(page)
    await asyncio.sleep(random.uniform(1, 3))
    if random.random() < 0.3:
        width = random.randint(800, 1600)
        height = random.randint(600, 1000)
        await page.set_viewport_size({"width": width, "height": height})
    await random_network_throttling(page)
    await fake_http_traffic(page)
    await generate_realistic_mouse_physics(page)
    await simulate_reading_patterns(page)
    for _ in range(random.randint(2, 5)):
        viewport = page.viewport_size or {"width": 1024, "height": 768}
        x = random.randint(0, viewport['width'])
        y = random.randint(0, viewport['height'])
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.1, 0.5))


###############################
# CAPTCHA Prevention
###############################
async def handle_captcha(page: Any) -> None:
    """
    Detect and neutralize CAPTCHA challenges.

    Hides CAPTCHA if found and then reloads the page.

    :param page: The Playwright page instance.
    """
    if await page.query_selector('#captcha-challenge'):
        await page.evaluate("""
            () => {
                document.querySelectorAll('iframe').forEach(iframe => {
                    if(iframe.src.includes('captcha')) {
                        iframe.style.display = 'none';
                    }
                });
            }
        """)
        await page.wait_for_timeout(5000)
        await page.reload()


###############################
# Session Rotation
###############################
async def rotate_session(context: Any) -> bool:
    """
    Randomly rotate session cookies from stored session files.

    :param context: The Playwright browser context.
    :return: True if a session was rotated successfully, otherwise False.
    """
    session_files = [f for f in os.listdir(SESSION_STORE_DIR) if f.startswith('linkedin_session_')]
    if session_files:
        selected_session = random.choice(session_files)
        storage = await load_storage(os.path.join(SESSION_STORE_DIR, selected_session))
        if storage:
            await context.add_cookies(storage)
            await context.storage_state(path=os.path.join(SESSION_STORE_DIR, selected_session))
            logger.debug(f"Rotated session: {selected_session}")
            return True
    return False


###############################
# Advanced Block Detection & Evasive Action
###############################
async def perform_evasive_action(page: Any, confidence: float) -> None:
    """
    Perform evasive measures if blocking indicators are detected.

    :param page: The Playwright page instance.
    :param confidence: A value representing the likelihood of being blocked.
    """
    logger.info(f"Performing evasive action with confidence {confidence}")
    await page.wait_for_timeout(5000)
    await page.reload()


async def detect_blocks(page: Any) -> bool:
    """
    Check for signs of automated blocking and trigger evasive actions if necessary.

    :param page: The Playwright page instance.
    :return: True if a block was detected and handled, otherwise False.
    """
    block_indicators = [
        ('text=unusual activity detected', 0.9),
        ('#error-for-alerts', 0.7),
        ('text=security check', 0.8)
    ]
    for selector, confidence in block_indicators:
        if await page.query_selector(selector):
            await perform_evasive_action(page, confidence)
            return True
    return False


###############################
# Traffic Light Monitoring
###############################
class TrafficLight:
    """
    Monitors page performance metrics as a traffic light system to adjust scraping behavior.
    """
    def __init__(self) -> None:
        self.status: str = 'green'
        self.request_count: int = 0

    async def monitor(self, page: Any) -> None:
        """
        Periodically check page performance metrics and adjust the traffic light status.

        :param page: The Playwright page instance.
        """
        self.request_count += 1
        if self.request_count % 50 == 0:
            performance_data = await page.evaluate("""
                () => {
                    const timing = performance.timing;
                    return {
                        dns: timing.domainLookupEnd - timing.domainLookupStart,
                        tcp: timing.connectEnd - timing.connectStart,
                        request: timing.responseStart - timing.requestStart,
                        domComplete: timing.domComplete - timing.domLoading
                    };
                }
            """)
            logger.info(f"Performance data: {performance_data}")
            if random.random() < 0.3:
                self.status = 'yellow'
                await page.wait_for_timeout(30000)
            elif random.random() < 0.1:
                self.status = 'red'
                await page.context.close()


def debug_log(message: str) -> None:
    """
    Log a debug message.

    :param message: The message to log.
    """
    logger.debug(f"[DEBUG] {message}")


async def scroll_randomly(page: Any) -> None:
    """
    Randomly scroll the page to simulate human behavior.

    :param page: The Playwright page instance.
    """
    for _ in range(random.randint(1, 5)):
        scroll_distance = random.randint(-400, 400)
        await page.mouse.wheel(0, scroll_distance)
        await asyncio.sleep(random.uniform(0.3, 1.2))


async def type_like_human(page: Any, selector: str, text: str) -> None:
    """
    Simulate human-like typing for a given selector.

    :param page: The Playwright page instance.
    :param selector: The CSS selector of the input element.
    :param text: The text to type.
    """
    for char in text:
        await page.type(selector, char)
        await asyncio.sleep(random.uniform(0.03, 0.25))
        if random.random() < 0.1:
            await asyncio.sleep(random.uniform(0.5, 2.0))


async def move_mouse_and_click(page: Any, selector: str) -> None:
    """
    Simulate realistic mouse movements and click an element specified by the selector.

    :param page: The Playwright page instance.
    :param selector: The CSS selector of the target element.
    """
    element = await page.query_selector(selector)
    if element:
        box = await element.bounding_box()
        if box:
            for _ in range(random.randint(3, 8)):
                x_offset = random.uniform(-20, 20)
                y_offset = random.uniform(-20, 20)
                await page.mouse.move(
                    box['x'] + box['width'] / 2 + x_offset,
                    box['y'] + box['height'] / 2 + y_offset,
                    steps=random.randint(3, 10)
                )
                await asyncio.sleep(random.uniform(0.05, 0.2))
            await page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
            await asyncio.sleep(random.uniform(0.2, 0.6))


async def load_session(page: Any, username: str) -> bool:
    """
    Load a saved session for a given username from disk and apply it to the current page context.

    :param page: The Playwright page instance.
    :param username: The username whose session to load.
    :return: True if the session was loaded successfully, otherwise False.
    """
    try:
        storage_file = os.path.join(SESSION_STORE_DIR, f'linkedin_session_{username}.json')
        storage = await load_storage(storage_file)
        if storage:
            await page.context.add_cookies(storage)
            debug_log(f"Session loaded from file for account: {username}")
            return True
        else:
            debug_log(f"No valid session found for account: {username}")
            return False
    except Exception as e:
        debug_log(f"Failed to load session for account {username}: {e}")
        return False


async def load_storage(storage_file: str) -> List[Any]:
    """
    Load session cookies from a storage file.

    :param storage_file: The path to the storage file.
    :return: A list of cookie dictionaries.
    """
    try:
        with open(storage_file) as f:
            storage = json.load(f)
            return storage.get('cookies', [])
    except FileNotFoundError:
        debug_log(f"Session file {storage_file} not found.")
        return []
    except json.JSONDecodeError:
        debug_log(f"Invalid JSON in session file: {storage_file}")
        return []


async def ensure_logged_in(page: Any, username: str, password: str) -> bool:
    """
    Ensure that the user is logged in to LinkedIn.

    Tries to load a saved session; if unavailable or invalid, performs a login process.

    :param page: The Playwright page instance.
    :param username: LinkedIn username.
    :param password: LinkedIn password.
    :return: True if login is successful, otherwise False.
    """
    try:
        await apply_stealth_scripts(page)
        session_loaded = await load_session(page, username)
        if session_loaded:
            await page.goto('https://www.linkedin.com/feed/', wait_until='domcontentloaded', timeout=20000)
            if await wait_for_feed(page):
                debug_log(f"Using saved session for account: {username}")
                return True
            else:
                debug_log(f"Saved session for account {username} did not fully load feed. Proceeding with login.")
        await page.goto('https://www.linkedin.com/login', wait_until='domcontentloaded')
        await simulate_human_behavior(page)
        username_input = await page.query_selector('input#username')
        if username_input:
            await type_like_human(page, 'input#username', username)
            await type_like_human(page, 'input#password', password)
        else:
            password_input = await page.query_selector('input#password')
            if password_input:
                await type_like_human(page, 'input#password', password)
            else:
                logger.error("Neither username nor password field found")
                return False
        await move_mouse_and_click(page, 'button[type="submit"]')
        if not await wait_for_feed(page):
            logger.error(f"Login failed for account {username}. Unexpected URL: {page.url}")
            return False
        logger.debug(f"Successfully logged in with account: {username}")
        await page.context.storage_state(path=os.path.join(SESSION_STORE_DIR, f'linkedin_session_{username}.json'))
        return True
    except Exception as e:
        logger.error(f"Login failed for account {username}: {e}")
        return False


def wait_for_feed(page: Any, timeout: int = 30000, interval: int = 1000) -> Any:
    """
    Return an asynchronous function that waits for the LinkedIn feed to load.

    :param page: The Playwright page instance.
    :param timeout: Maximum wait time in milliseconds.
    :param interval: Polling interval in milliseconds.
    :return: An awaitable function that returns True if the feed is loaded, otherwise False.
    """
    async def _wait() -> bool:
        waited = 0
        while waited < timeout:
            current_url = page.url
            if "linkedin.com/feed" in current_url:
                return True
            if "linkedin.com/ssr-login" in current_url:
                logger.info("Detected ssr-login page, waiting for 5 seconds...")
                await asyncio.sleep(5)
                waited += 5000
            else:
                await asyncio.sleep(interval / 1000)
                waited += interval
        return False
    return _wait()


def extract_emails_from_text(text: str) -> Optional[List[str]]:
    """
    Extract email addresses from a given text string.

    :param text: The input text.
    :return: A list of email addresses found, or None if none found.
    """
    if not text:
        return None
    email_regex = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
    return email_regex.findall(text)


def refined_clean_text(text: str) -> str:
    """
    Clean up a text string by reducing whitespace.

    :param text: The input text.
    :return: A cleaned-up string with excess whitespace removed.
    """
    return re.sub(r'\s+', ' ', text).strip()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def get_company_size(page: Any, url: str) -> str:
    """
    Scrape the company size from a given LinkedIn company page URL.

    :param page: The Playwright page instance.
    :param url: The URL of the LinkedIn company page.
    :return: The company size as a string, or "Unknown" if not found.
    """
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await simulate_human_behavior(page)
        not_found_selector = 'h1:has-text("Page not found")'
        if await page.query_selector(not_found_selector):
            logger.warning(f"Company page not found: {url}")
            return "Unknown"
        await page.wait_for_selector('.org-top-card-summary-info-list__info-item', timeout=20000)
        employee_count_element = await page.query_selector('.org-top-card-summary-info-list__info-item:has-text("employee")')
        if employee_count_element:
            size_text = await employee_count_element.inner_text()
            return size_text.replace("employees", "").strip()
        else:
            logger.warning(f"Employee count not found on page: {url}")
            return "Unknown"
    except Exception as e:
        logger.error(f"Error scraping company size from {url}: {str(e)}")
        return "Unknown"


def get_company_size_score(size_text: str) -> int:
    """
    Convert a company size text to a numeric score.

    :param size_text: Company size string.
    :return: An integer score representing the company size.
    """
    size_text = size_text.lower().strip()
    if "unknown" in size_text:
        return 0
    size_map = {
        "1-10": 1, "2-10": 1, "11-50": 2, "51-200": 3, "201-500": 4,
        "501-1k": 5, "501-1,000": 5, "1k-5k": 6, "1,001-5,000": 6,
        "5k-10k": 7, "5,001-10,000": 7, "10k+": 8, "10,001+": 8
    }
    for key, score in size_map.items():
        if key in size_text:
            return score
    return 0
