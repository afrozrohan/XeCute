
from urllib.parse import urlparse
import ipaddress

from playwright.sync_api import sync_playwright
from langchain_core.tools import tool

_playwright = None
_browser = None
_page = None


def _get_page():
    """Create a browser session if one does not exist."""
    global _playwright, _browser, _page

    if _page is None or _page.is_closed():
        if _playwright is None:
            _playwright = sync_playwright().start()

        _browser = _playwright.chromium.launch(headless=True)
        _page = _browser.new_page()

    return _page


def _validate_url(url: str) -> None:
    """Allow HTTP(S) URLs but reject obvious local/private targets."""
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only valid HTTP and HTTPS URLs are allowed.")

    hostname = parsed.hostname.lower()

    if hostname in ("localhost", "localhost.localdomain"):
        raise ValueError("Access to localhost is blocked.")

    try:
        address = ipaddress.ip_address(hostname)
        if not address.is_global:
            raise ValueError("Access to private or non-public IPs is blocked.")
    except ValueError as error:
        if "blocked" in str(error):
            raise


@tool
def browser_open(url: str) -> str:
    """Open a public HTTP or HTTPS webpage in the browser."""
    _validate_url(url)
    page = _get_page()

    response = page.goto(url, wait_until="domcontentloaded", timeout=30000)

    title = page.title()
    final_url = page.url
    status_code = response.status if response else "unknown"

    return (
        f"Page opened.\n"
        f"Title: {title}\n"
        f"URL: {final_url}\n"
        f"HTTP status: {status_code}"
    )


@tool
def browser_get_page_text() -> str:
    """Read visible text from the currently open webpage."""
    page = _get_page()
    text = page.locator("body").inner_text(timeout=10000)

    # Prevent excessively large pages from overwhelming the model.
    limit = 12000
    if len(text) > limit:
        text = text[:limit] + "\n...[content truncated]"

    return text


@tool
def browser_click(selector: str) -> str:
    """Click a visible webpage element using a CSS selector."""
    page = _get_page()
    element = page.locator(selector).first

    element.click(timeout=10000)

    return (
        f"Click completed.\n"
        f"Selector: {selector}\n"
        f"Current URL: {page.url}"
    )


@tool
def browser_fill(selector: str, value: str) -> str:
    """Fill a webpage input or textarea using a CSS selector."""
    page = _get_page()
    element = page.locator(selector).first

    element.fill(value, timeout=10000)

    return f"Input filled successfully. Selector: {selector}"


@tool
def browser_close() -> str:
    """Close the current browser session and release resources."""
    global _playwright, _browser, _page

    try:
        if _browser is not None:
            _browser.close()
    finally:
        if _playwright is not None:
            _playwright.stop()

        _playwright = None
        _browser = None
        _page = None

    return "Browser session closed."
