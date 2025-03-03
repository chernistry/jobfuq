import json
import random
import asyncio
import re
import os
from typing import Any, Dict, List, Optional
from playwright.async_api import Route, Request, BrowserType
from faker import Faker

from jobfuq.logger.logger import logger
from jobfuq.utils.utils import load_config

# Session storage directory
SESSION_STORE_DIR: str = "session_store"
try:
    main_config: Dict[str, Any] = load_config("jobfuq/conf/config.toml")
    session_override: Optional[str] = main_config.get("sessions", {}).get("store_dir")
    if session_override:
        SESSION_STORE_DIR = session_override
except Exception as e:
    logger.debug(f"Could not load session override from main config: {e}")

os.makedirs(SESSION_STORE_DIR, exist_ok=True)

scraping_mode: str = main_config.get("scraping", {}).get("mode", "normal").lower()
linked_config: Dict[str, Any] = load_config("jobfuq/conf/linked_config.toml")


async def create_stealth_browser(browser_type: BrowserType) -> Any:
    fake = Faker()
    b_args: Dict[str, Any] = linked_config.get("browser_args", {})
    width_min: int = b_args.get("window_width_min", 1000)
    width_max: int = b_args.get("window_width_max", 1600)
    height_min: int = b_args.get("window_height_min", 800)
    height_max: int = b_args.get("window_height_max", 1200)
    window_size: str = f"{random.randint(width_min, width_max)},{random.randint(height_min, height_max)}"
    timezones: List[str] = linked_config.get("env", {}).get("timezones", ["America/New_York"])
    return await browser_type.launch(
        headless=main_config.get("headless", False),
        args=[
            f"--user-agent={fake.user_agent()}",
            f"--disable-blink-features={b_args.get('disable_blink_features', 'AutomationControlled')}",
            "--disable-web-security",
            f"--disable-features={b_args.get('disable_features', 'IsolateOrigins,site-per-process')}",
            f"--window-size={window_size}",
        ],
        env={"TZ": random.choice(timezones)},
    )


async def apply_stealth_scripts(page: Any) -> None:
    # Prevent WebRTC leaks
    await page.add_init_script("() => { window.RTCPeerConnection = undefined; }")
    # AudioContext masking
    await page.add_init_script("() => { window.AudioContext = undefined; }")


async def block_resources(route: Route, request: Request) -> None:
    block_types: List[str] = linked_config.get("resource_blocking", {}).get("types", ["image", "font", "media"])
    if request.resource_type in block_types:
        await route.abort()
    else:
        await route.continue_()


async def random_network_throttling(page: Any) -> None:
    profiles: List[str] = linked_config.get("network_profiles", {}).get("profiles", ["Wi-Fi", "Regular4G", "DSL"])
    chosen: str = random.choice(profiles)
    logger.debug(f"[STEALTH] Applying random network profile: {chosen}")
    await page.context.set_geolocation({
        "latitude": random.uniform(-90, 90),
        "longitude": random.uniform(-180, 180),
    })


async def fake_http_traffic(page: Any) -> None:
    fake_http_url: str = linked_config.get("fake_http", {}).get("url", "https://httpbin.org/get")
    try:
        response = await page.context.request.get(
            fake_http_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        logger.debug(f"Fake HTTP traffic response: {response.status} - {await response.text()}")
    except Exception as e:
        logger.debug(f"Fake HTTP traffic error: {e}")


async def generate_realistic_mouse_physics(page: Any) -> None:
    physics: Dict[str, Any] = linked_config.get("mouse_physics", {})
    steps_min: int = physics.get("steps_min", 120)
    steps_max: int = physics.get("steps_max", 250)
    gravity_min: float = physics.get("gravity_min", 0.5)
    gravity_max: float = physics.get("gravity_max", 1.5)
    wind_min: float = physics.get("wind_min", -0.5)
    wind_max: float = physics.get("wind_max", 0.5)
    for _ in range(random.randint(3, 7)):
        start_x: int = random.randint(0, 1200)
        start_y: int = random.randint(0, 800)
        end_x: int = random.randint(0, 1200)
        end_y: int = random.randint(0, 800)
        steps: int = random.randint(steps_min, steps_max)
        gravity: float = random.uniform(gravity_min, gravity_max)
        wind: float = random.uniform(wind_min, wind_max)
        for i in range(steps):
            t: float = i / steps
            current_x: float = start_x + (end_x - start_x) * t
            current_y: float = start_y + (end_y - start_y) * t + gravity * t * wind
            await page.mouse.move(current_x, current_y)
            await asyncio.sleep(random.uniform(0.001, 0.005))


async def simulate_reading_patterns(page: Any) -> None:
    for _ in range(random.randint(1, 3)):
        scroll_distance: int = random.randint(200, 600)
        await page.mouse.wheel(0, scroll_distance)
        await asyncio.sleep(random.uniform(1, 2))


async def scroll_randomly(page: Any) -> None:
    for _ in range(random.randint(1, 5)):
        scroll_distance: int = random.randint(-400, 400)
        await page.mouse.wheel(0, scroll_distance)
        await asyncio.sleep(random.uniform(0.3, 1.2))


async def simulate_human_behavior(page: Any) -> None:
    if scraping_mode == "aggressive":
        logger.debug("[AGGRESSIVE MODE] Skipping stealth/human steps.")
        return
    if scraping_mode == "normal":
        logger.debug("[NORMAL MODE] Doing moderate stealth.")
        await scroll_randomly(page)
        await asyncio.sleep(random.uniform(1, 2))
        return
    logger.debug("[STEALTH MODE] Applying full advanced stealth.")
    await scroll_randomly(page)
    await asyncio.sleep(random.uniform(1, 3))
    if random.random() < 0.3:
        width: int = random.randint(800, 1600)
        height: int = random.randint(600, 1000)
        await page.set_viewport_size({"width": width, "height": height})
    await random_network_throttling(page)
    await fake_http_traffic(page)
    await generate_realistic_mouse_physics(page)
    await simulate_reading_patterns(page)
    for _ in range(random.randint(2, 5)):
        viewport: Dict[str, int] = page.viewport_size or {"width": 1024, "height": 768}
        x: int = random.randint(0, viewport["width"])
        y: int = random.randint(0, viewport["height"])
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.1, 0.5))


async def handle_manual_captcha(page: Any, playwright: Any, config: Dict[str, Any]) -> Any:
    if config.get("headless", False):
        logger.info(
            "Detected captcha/ checkpoint in headless mode. Restarting browser in headful mode for captcha solving."
        )
        storage_state = await page.context.storage_state()
        current_browser = page.context.browser
        await current_browser.close()

        browser_headful = await playwright.chromium.launch(headless=False, slow_mo=50)
        context_headful = await browser_headful.new_context(storage_state=storage_state)
        new_page = await context_headful.new_page()
        await new_page.goto(page.url, wait_until="domcontentloaded")
        logger.info("Browser switched to headful mode. Solve the captcha manually in the opened window.")
        await asyncio.to_thread(input, "Press Enter after you have solved the captcha and the page has loaded...")
        updated_state = await context_headful.storage_state()
        await browser_headful.close()
        logger.info("Captcha solved. Relaunching headless browser with updated session state.")
        headless_browser = await playwright.chromium.launch(headless=True, slow_mo=50)
        new_context = await headless_browser.new_context(storage_state=updated_state)
        new_page2 = await new_context.new_page()
        feed_url: str = linked_config.get("urls", {}).get("feed_url", "https://www.linkedin.com/feed/")
        await new_page2.goto(feed_url, wait_until="domcontentloaded")
        logger.info("Switched back to headless mode. Continuing with new context.")
        return new_page2
    else:
        logger.info("Checkpoint/captcha encountered in headful mode. Please solve it manually, then press Enter...")
        await asyncio.to_thread(input, "Press Enter after solving the captcha...")
        return page


async def wait_for_feed(page: Any, playwright: Any, config: Dict[str, Any], timeout: int = 30000, interval: int = 1000) -> Optional[Any]:
    waited: int = 0
    feed_indicator: str = linked_config.get("urls", {}).get("feed_url_indicator", "linkedin.com/feed")
    ssr_login_indicator: str = linked_config.get("urls", {}).get("ssr_login_indicator", "linkedin.com/ssr-login")
    checkpoint_indicator: str = linked_config.get("urls", {}).get("checkpoint_indicator", "checkpoint/challenge")
    current_page: Any = page
    while waited < timeout:
        if feed_indicator in current_page.url:
            return current_page
        if checkpoint_indicator in current_page.url:
            logger.info("Detected checkpoint challenge. Initiating captcha handling procedure.")
            current_page = await handle_manual_captcha(current_page, playwright, config)
            await asyncio.sleep(2)
        elif ssr_login_indicator in current_page.url:
            logger.info("Detected ssr-login page, waiting 5 seconds...")
            await asyncio.sleep(5)
            waited += 5000
        else:
            await asyncio.sleep(interval / 1000)
            waited += interval
    return None


async def type_like_human(page: Any, selector: str, text: str) -> None:
    for char in text:
        await page.type(selector, char)
        await asyncio.sleep(random.uniform(0.03, 0.25))
        if random.random() < 0.1:
            await asyncio.sleep(random.uniform(0.5, 1.5))


async def move_mouse_and_click(page: Any, selector: str) -> None:
    element = await page.query_selector(selector)
    if element:
        box: Optional[Dict[str, float]] = await element.bounding_box()
        if box:
            for _ in range(random.randint(3, 5)):
                x_offset: float = random.uniform(-10, 10)
                y_offset: float = random.uniform(-10, 10)
                await page.mouse.move(
                    box["x"] + box["width"] / 2 + x_offset,
                    box["y"] + box["height"] / 2 + y_offset,
                    steps=random.randint(3, 10),
                    )
                await asyncio.sleep(random.uniform(0.05, 0.2))
            await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            await asyncio.sleep(random.uniform(0.2, 0.6))


def extract_emails_from_text(text: str) -> Optional[List[str]]:
    if not text:
        return None
    email_regex = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
    return email_regex.findall(text)


def refined_clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


async def load_storage(storage_file: str) -> List[Any]:
    try:
        with open(storage_file) as f:
            storage = json.load(f)
            return storage.get("cookies", [])
    except (FileNotFoundError, json.JSONDecodeError):
        logger.debug(f"Session file not found or invalid JSON: {storage_file}")
        return []


async def load_session(page: Any, username: str) -> bool:
    storage_file: str = os.path.join(SESSION_STORE_DIR, f"linkedin_session_{username}.json")
    storage = await load_storage(storage_file)
    if storage:
        await page.context.add_cookies(storage)
        logger.debug(f"Session loaded from file for account: {username}")
        return True
    else:
        logger.debug(f"No valid session found for account: {username}")
        return False


async def rotate_session(context: Any) -> bool:
    session_files: List[str] = [
        f for f in os.listdir(SESSION_STORE_DIR) if f.startswith("linkedin_session_")
    ]
    if session_files:
        selected_session: str = random.choice(session_files)
        storage = await load_storage(os.path.join(SESSION_STORE_DIR, selected_session))
        if storage:
            await context.add_cookies(storage)
            await context.storage_state(path=os.path.join(SESSION_STORE_DIR, selected_session))
            logger.debug(f"Rotated session: {selected_session}")
            return True
    return False


async def ensure_logged_in(
        page: Any, username: str, password: str, playwright: Any, config: Dict[str, Any]
) -> Optional[Any]:
    try:
        await apply_stealth_scripts(page)
        session_loaded: bool = await load_session(page, username)
        urls_conf: Dict[str, Any] = linked_config.get("urls", {})
        feed_url: str = urls_conf.get("feed_url", "https://www.linkedin.com/feed/")
        login_url: str = urls_conf.get("login_url", "https://www.linkedin.com/login")
        wait_until: str = urls_conf.get("wait_until", "domcontentloaded")
        if session_loaded:
            await page.goto(feed_url, wait_until=wait_until, timeout=750000)
            new_page: Optional[Any] = await wait_for_feed(page, playwright, config)
            if new_page:
                logger.debug(f"Using saved session for account: {username}")
                return new_page
            else:
                logger.debug(f"Session for {username} didn't load feed. Logging in fresh.")
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
                return None
        await move_mouse_and_click(page, 'button[type="submit"]')
        new_page = await wait_for_feed(page, playwright, config)
        if not new_page:
            logger.error(f"Login failed for account {username}. Unexpected URL: {page.url}")
            return None
        logger.debug(f"Successfully logged in: {username}")
        session_path: str = os.path.join(SESSION_STORE_DIR, f"linkedin_session_{username}.json")
        await page.context.storage_state(path=session_path)
        return new_page
    except Exception as e:
        logger.error(f"Login failed for account {username}: {e}")
        return None