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

SESSION_STORE_DIR = "session_store"

try:
    main_config = load_config("jobfuq/conf/config.toml")
    session_override = main_config.get("sessions", {}).get("store_dir")
    if session_override:
        SESSION_STORE_DIR = session_override
except Exception as e:
    logger.debug(f"Could not load session override from main config: {e}")

os.makedirs(SESSION_STORE_DIR, exist_ok=True)

scraping_mode = main_config.get("scraping", {}).get("mode", "normal").lower()
linked_config = load_config("jobfuq/conf/linked_config.toml")


async def create_stealth_browser(browser_type: BrowserType) -> Any:
    """
    Launches a stealth browser instance with random user-agent, viewport,
    and other anti-fingerprinting measures.
    """
    fake = Faker()
    b_args = linked_config.get("browser_args", {})
    width_min = b_args.get("window_width_min", 1000)
    width_max = b_args.get("window_width_max", 1600)
    height_min = b_args.get("window_height_min", 800)
    height_max = b_args.get("window_height_max", 1200)
    window_size = f"{random.randint(width_min, width_max)},{random.randint(height_min, height_max)}"
    timezones = linked_config.get("env", {}).get("timezones", ["America/New_York"])

    return await browser_type.launch(
        headless=False,
        args=[
            f"--user-agent={fake.user_agent()}",
            f"--disable-blink-features={b_args.get('disable_blink_features','AutomationControlled')}",
            "--disable-web-security",
            f"--disable-features={b_args.get('disable_features','IsolateOrigins,site-per-process')}",
            f"--window-size={window_size}"
        ],
        env={"TZ": random.choice(timezones)}
    )


async def apply_stealth_scripts(page: Any) -> None:
    """
    Inject JavaScript to mask WebRTC and fingerprinting signals.
    """
    # Prevent WebRTC leaks
    await page.add_init_script("() => { window.RTCPeerConnection = undefined; }")
    # Canvas fingerprint spoof
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


async def block_resources(route: Route, request: Request) -> None:
    """
    Blocks images, fonts, or media resources to speed up scraping.
    """
    block_types = linked_config.get("resource_blocking", {}).get("types", ["image", "font", "media"])
    if request.resource_type in block_types:
        await route.abort()
    else:
        await route.continue_()


async def random_network_throttling(page: Any) -> None:
    """
    Randomly apply geolocation to simulate different networks.
    """
    profiles = linked_config.get("network_profiles", {}).get("profiles", ["Wi-Fi", "Regular4G", "DSL"])
    chosen = random.choice(profiles)
    logger.debug(f"[STEALTH] Applying random network profile: {chosen}")
    await page.context.set_geolocation({
        "latitude": random.uniform(-90, 90),
        "longitude": random.uniform(-180, 180)
    })


async def fake_http_traffic(page: Any) -> None:
    """
    Generate benign HTTP requests from the same context to appear more human-like.
    Fixed: Removed redundant context creation and corrected request handling
    """
    fake_http_url = linked_config.get("fake_http", {}).get("url", "https://httpbin.org/get")

    try:
        # Directly use existing request context from page
        response = await page.context.request.get(
            fake_http_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml"
            }
        )
        logger.debug(f"Fake HTTP traffic response: {response.status} - {await response.text()}")

    except Exception as e:
        logger.debug(f"Fake HTTP traffic error: {e}")
        # Implement exponential backoff for retries
        await asyncio.sleep(2 ** retry_count)



async def generate_realistic_mouse_physics(page: Any) -> None:
    """
    Move the mouse in physics-based arcs to fool bot detection systems.
    """
    physics = linked_config.get("mouse_physics", {})
    steps_min = physics.get("steps_min", 120)
    steps_max = physics.get("steps_max", 250)
    gravity_min = physics.get("gravity_min", 0.5)
    gravity_max = physics.get("gravity_max", 1.5)
    wind_min = physics.get("wind_min", -0.5)
    wind_max = physics.get("wind_max", 0.5)

    for _ in range(random.randint(3, 7)):
        start_x = random.randint(0, 1200)
        start_y = random.randint(0, 800)
        end_x = random.randint(0, 1200)
        end_y = random.randint(0, 800)
        steps = random.randint(steps_min, steps_max)
        gravity = random.uniform(gravity_min, gravity_max)
        wind = random.uniform(wind_min, wind_max)

        for i in range(steps):
            t = i / steps
            current_x = start_x + (end_x - start_x) * t
            current_y = start_y + (end_y - start_y) * t + gravity * t * wind
            await page.mouse.move(current_x, current_y)
            await asyncio.sleep(random.uniform(0.001, 0.005))


async def simulate_reading_patterns(page: Any) -> None:
    """
    Perform slow scrolling to mimic reading.
    """
    for _ in range(random.randint(1, 3)):
        scroll_distance = random.randint(200, 600)
        await page.mouse.wheel(0, scroll_distance)
        await asyncio.sleep(random.uniform(1, 2))


async def scroll_randomly(page: Any) -> None:
    """
    Scroll unpredictably to appear more human.
    """
    for _ in range(random.randint(1, 5)):
        scroll_distance = random.randint(-400, 400)
        await page.mouse.wheel(0, scroll_distance)
        await asyncio.sleep(random.uniform(0.3, 1.2))


async def simulate_human_behavior(page: Any) -> None:
    """
    Decide how much stealth/humanlike behavior to apply, based on scraping_mode in config.
    """
    if scraping_mode == "aggressive":
        logger.debug("[AGGRESSIVE MODE] Skipping stealth/human steps.")
        return

    if scraping_mode == "normal":
        logger.debug("[NORMAL MODE] Doing moderate stealth.")
        await scroll_randomly(page)
        await asyncio.sleep(random.uniform(1, 2))
        return

    # Full stealth
    logger.debug("[STEALTH MODE] Applying full advanced stealth.")
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
        x = random.randint(0, viewport["width"])
        y = random.randint(0, viewport["height"])
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.1, 0.5))


async def handle_captcha(page: Any) -> None:
    """
    Detect and neutralize CAPTCHAs if they appear.
    """
    captcha_selector = linked_config.get("captcha", {}).get("selector", "#captcha-challenge")
    if await page.query_selector(captcha_selector):
        await page.evaluate("""
            () => {
                document.querySelectorAll('iframe').forEach(iframe => {
                    if (iframe.src.includes('captcha')) {
                        iframe.style.display = 'none';
                    }
                });
            }
        """)
        timeout_val = linked_config.get("captcha", {}).get("timeout_ms", 5000)
        await page.wait_for_timeout(timeout_val)
        await page.reload()


async def detect_blocks(page: Any) -> bool:
    """
    Check for known block signals. If found, trigger evasive action.
    """
    block_indicators = linked_config.get("block_indicators", {}).get(
        "selectors",
        [
            ("text=unusual activity detected", 0.9),
            ("#error-for-alerts", 0.7),
            ("text=security check", 0.8),
        ]
    )
    for (selector, confidence) in block_indicators:
        if await page.query_selector(selector):
            await perform_evasive_action(page, confidence)
            return True
    return False


async def perform_evasive_action(page: Any, confidence: float) -> None:
    """
    Wait and reload when blocking is detected.
    """
    logger.info(f"Performing evasive action with confidence {confidence}")
    evasive_timeout = linked_config.get("evasive", {}).get("timeout_ms", 5000)
    await page.wait_for_timeout(evasive_timeout)
    await page.reload()


async def rotate_session(context: Any) -> bool:
    """
    Load and apply a random stored session from session_store/.
    """
    session_files = [f for f in os.listdir(SESSION_STORE_DIR) if f.startswith("linkedin_session_")]
    if session_files:
        selected_session = random.choice(session_files)
        storage = await load_storage(os.path.join(SESSION_STORE_DIR, selected_session))
        if storage:
            await context.add_cookies(storage)
            await context.storage_state(path=os.path.join(SESSION_STORE_DIR, selected_session))
            logger.debug(f"Rotated session: {selected_session}")
            return True
    return False


async def load_session(page: Any, username: str) -> bool:
    """
    Attempt to load an existing session for a given username.
    """
    storage_file = os.path.join(SESSION_STORE_DIR, f"linkedin_session_{username}.json")
    storage = await load_storage(storage_file)
    if storage:
        await page.context.add_cookies(storage)
        logger.debug(f"Session loaded from file for account: {username}")
        return True
    else:
        logger.debug(f"No valid session found for account: {username}")
        return False


async def load_storage(storage_file: str) -> List[Any]:
    """
    Load cookie storage from JSON file.
    """
    try:
        with open(storage_file) as f:
            storage = json.load(f)
            return storage.get("cookies", [])
    except (FileNotFoundError, json.JSONDecodeError):
        logger.debug(f"Session file not found or invalid JSON: {storage_file}")
        return []


async def ensure_logged_in(page: Any, username: str, password: str) -> bool:
    """
    Log in to LinkedIn, using a saved session if available.
    """
    try:
        await apply_stealth_scripts(page)
        session_loaded = await load_session(page, username)

        urls_conf = linked_config.get("urls", {})
        feed_url = urls_conf.get("feed_url", "https://www.linkedin.com/feed/")
        login_url = urls_conf.get("login_url", "https://www.linkedin.com/login")
        wait_until = urls_conf.get("wait_until", "domcontentloaded")

        if session_loaded:
            await page.goto(feed_url, wait_until=wait_until, timeout=20000)
            if await wait_for_feed(page):
                logger.debug(f"Using saved session for account: {username}")
                return True
            else:
                logger.debug(f"Saved session for {username} did not load feed. Logging in fresh.")

        await page.goto(login_url, wait_until=wait_until)
        await simulate_human_behavior(page)

        username_input = await page.query_selector("input#username")
        if username_input:
            await type_like_human(page, "input#username", username)
            await type_like_human(page, "input#password", password)
        else:
            password_input = await page.query_selector("input#password")
            if password_input:
                await type_like_human(page, "input#password", password)
            else:
                logger.error("Neither username nor password field found!")
                return False

        await move_mouse_and_click(page, 'button[type="submit"]')

        if not await wait_for_feed(page):
            logger.error(f"Login failed for account {username}. Unexpected URL: {page.url}")
            return False

        logger.debug(f"Successfully logged in: {username}")
        session_path = os.path.join(SESSION_STORE_DIR, f"linkedin_session_{username}.json")
        await page.context.storage_state(path=session_path)
        return True

    except Exception as e:
        logger.error(f"Login failed for account {username}: {e}")
        return False


def wait_for_feed(page: Any, timeout: int = 30000, interval: int = 1000) -> Any:
    """
    Return an async function that polls the URL for feed page presence.
    """
    async def _wait() -> bool:
        waited = 0
        feed_indicator = linked_config.get("urls", {}).get("feed_url_indicator", "linkedin.com/feed")
        ssr_login_indicator = linked_config.get("urls", {}).get("ssr_login_indicator", "linkedin.com/ssr-login")

        while waited < timeout:
            current_url = page.url
            if feed_indicator in current_url:
                return True
            if ssr_login_indicator in current_url:
                logger.info("Detected ssr-login page, waiting 5 seconds...")
                await asyncio.sleep(5)
                waited += 5000
            else:
                await asyncio.sleep(interval / 1000)
                waited += interval
        return False
    return _wait()


async def type_like_human(page: Any, selector: str, text: str) -> None:
    """
    Type text with random intervals, approximating human input.
    """
    for char in text:
        await page.type(selector, char)
        await asyncio.sleep(random.uniform(0.03, 0.25))
        if random.random() < 0.1:
            await asyncio.sleep(random.uniform(0.5, 1.5))


async def move_mouse_and_click(page: Any, selector: str) -> None:
    """
    Move mouse gradually and click the given selector.
    """
    element = await page.query_selector(selector)
    if element:
        box = await element.bounding_box()
        if box:
            for _ in range(random.randint(3, 5)):
                x_offset = random.uniform(-10, 10)
                y_offset = random.uniform(-10, 10)
                await page.mouse.move(
                    box["x"] + box["width"] / 2 + x_offset,
                    box["y"] + box["height"] / 2 + y_offset,
                    steps=random.randint(3, 10)
                )
                await asyncio.sleep(random.uniform(0.05, 0.2))
            await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            await asyncio.sleep(random.uniform(0.2, 0.6))


def extract_emails_from_text(text: str) -> Optional[List[str]]:
    """
    Extract emails from text using a simple regex.
    """
    if not text:
        return None
    email_regex = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
    return email_regex.findall(text)


def refined_clean_text(text: str) -> str:
    """
    Clean text by converting multiple spaces to single.
    """
    return re.sub(r"\s+", " ", text).strip()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def get_company_size(page: Any, url: str) -> str:
    """
    Attempt to scrape the company size from the LinkedIn page.
    Retries on failure up to 3 times with exponential backoff.
    """
    try:
        await page.goto(url, wait_until=linked_config.get("urls", {}).get("wait_until", "domcontentloaded"), timeout=30000)
        await simulate_human_behavior(page)

        not_found_selector = linked_config.get("company", {}).get("not_found_selector", 'h1:has-text("Page not found")')
        if await page.query_selector(not_found_selector):
            logger.warning(f"Company page not found: {url}")
            return "Unknown"

        emp_sel = linked_config.get("company", {}).get("employee_selector", ".org-top-card-summary-info-list__info-item")
        await page.wait_for_selector(emp_sel, timeout=20000)

        # If there's a sub-selector with "employee" text
        sub_sel = linked_config.get("company", {}).get(
            "employee_selector",
            ".org-top-card-summary-info-list__info-item:has-text(\"employee\")"
        )
        employee_count_element = await page.query_selector(sub_sel)
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
    Convert a textual company size into a numeric score.
    """
    size_text = size_text.lower().strip()
    if "unknown" in size_text:
        return 0

    size_map = linked_config.get("size_map", {})
    for (key, score) in size_map.items():
        if key in size_text:
            return score
    return 0