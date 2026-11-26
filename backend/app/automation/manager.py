"""Playwright browser lifecycle and execution manager for CarryFlow."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from backend.app.preparation.security import UnsafeDestination, extract_https_origin, resolve_public_addresses, validate_public_https_url

logger = logging.getLogger("carryflow.automation.manager")

ALLOWED_AUTOMATION_ORIGINS = {
    "http://127.0.0.1:8000",
    "http://localhost:8000",
}


def is_allowed_origin(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return False
        port = parsed.port
        origin = f"{parsed.scheme}://{parsed.hostname.lower()}" + (f":{port}" if port else "")
        return origin in ALLOWED_AUTOMATION_ORIGINS
    except Exception:
        return False


class PlaywrightManager:
    """Manages the lifecycle of an async Playwright browser instance."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()
        self._latest_screenshot: bytes | None = None
        self._current_url: str = "about:blank"
        self._is_ready: bool = False
        self._error: str | None = None
        self._approved_origins = set(ALLOWED_AUTOMATION_ORIGINS)
        self._navigation_origin: str | None = None
        self._pinned_host: str | None = None
        self._pinned_ip: str | None = None
        self._allow_document_navigation = False
        self._expected_document_url: str | None = None
        self._initial_document_seen = False
        self._block_page_egress = False

    def approve_origin(self, origin: str) -> None:
        self._approved_origins.add(origin.rstrip("/").lower())

    def lock_page_egress(self) -> None:
        """Stop page-initiated requests before writing any Passport value into controls."""
        self._block_page_egress = True

    @property
    def is_running(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    @property
    def is_ready(self) -> bool:
        return self._is_ready and self.is_running

    @property
    def current_url(self) -> str:
        return self._current_url

    @property
    def latest_screenshot(self) -> bytes | None:
        return self._latest_screenshot

    async def start(
        self,
        headless: bool | None = None,
        slow_mo: int | None = None,
    ) -> bool:
        """Start Playwright and launch Chromium."""
        async with self._lock:
            if self.is_running and self._page:
                return True

            if headless is None:
                headless_env = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower()
                headless = headless_env not in {"false", "0", "no"}

            if slow_mo is None:
                try:
                    slow_mo = int(os.getenv("PLAYWRIGHT_SLOWMO", "50"))
                except ValueError:
                    slow_mo = 50

            try:
                launch_args = []
                if self._pinned_host and self._pinned_ip:
                    launch_args.append(f"--host-resolver-rules=MAP {self._pinned_host} {self._pinned_ip},EXCLUDE localhost")
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=headless,
                    slow_mo=slow_mo,
                    args=launch_args,
                )
                self._context = await self._browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent="CarryFlow-BrowserTaskAssistant/0.1.0",
                    service_workers="block",
                )
                async def _block_websocket(websocket_route) -> None:
                    await websocket_route.close(code=1008, reason="CarryFlow blocks WebSocket connections")
                await self._context.route_web_socket("**/*", _block_websocket)
                self._page = await self._context.new_page()
                self._page.set_default_timeout(5_000)
                self._page.set_default_navigation_timeout(15_000)
                await self._page.add_init_script("""(() => {
                  const block = e => { e.preventDefault(); e.stopImmediatePropagation(); };
                  document.addEventListener('submit', block, true);
                  HTMLFormElement.prototype.submit = function(){ throw new Error('CarryFlow blocks form submission'); };
                  HTMLFormElement.prototype.requestSubmit = function(){ throw new Error('CarryFlow blocks form submission'); };
                  window.fetch = () => Promise.reject(new Error('CarryFlow blocks page network actions'));
                  const open = XMLHttpRequest.prototype.open;
                  XMLHttpRequest.prototype.open = function(method, ...args) { if (!['GET','HEAD'].includes(String(method).toUpperCase())) throw new Error('CarryFlow blocks page write requests'); return open.call(this, method, ...args); };
                  XMLHttpRequest.prototype.send = function(){ throw new Error('CarryFlow blocks page network actions'); };
                  if (navigator.sendBeacon) navigator.sendBeacon = () => false;
                  window.WebSocket = function(){ throw new Error('CarryFlow blocks page network actions'); };
                  window.EventSource = function(){ throw new Error('CarryFlow blocks page network actions'); };
                  window.RTCPeerConnection = function(){ throw new Error('CarryFlow blocks peer-to-peer network actions'); };
                  if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = window.RTCPeerConnection;
                })();""")
                await self._context.route("**/*", self._guard_request)
                self._current_url = self._page.url or "about:blank"
                self._is_ready = True
                self._error = None
                logger.info("PlaywrightManager started successfully")
                return True
            except Exception as e:
                self._error = str(e)
                self._is_ready = False
                logger.exception("Failed to start Playwright: %s", e)
                await self._cleanup()
                return False

    async def stop(self) -> None:
        """Stop and cleanup Playwright browser instance."""
        async with self._lock:
            await self._cleanup()

    async def _cleanup(self) -> None:
        if self._page:
            try:
                await self._page.close()
            except Exception:
                pass
            self._page = None

        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None

        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        self._is_ready = False
        self._block_page_egress = False

    async def get_page(self) -> Page:
        """Get active page or ensure one exists."""
        if not self.is_running or not self._page or self._page.is_closed():
            started = await self.start()
            if not started or not self._page:
                raise RuntimeError(f"Playwright browser is not running: {self._error or 'Unknown error'}")
        return self._page

    async def navigate(self, url: str) -> dict[str, Any]:
        """Navigate active page to URL with strict origin enforcement."""
        try:
            parsed = urlsplit(url)
            if parsed.scheme == "https":
                origin = extract_https_origin(url)
                if origin not in self._approved_origins:
                    raise ValueError("Destination origin has not been explicitly approved.")
                origin, url = validate_public_https_url(url)
                host, addresses = await asyncio.to_thread(resolve_public_addresses, url)
                if self._pinned_host != host or self._pinned_ip not in addresses:
                    await self.stop()
                    self._pinned_host, self._pinned_ip = host, addresses[0]
                    if not await self.start():
                        raise ValueError("Could not start a browser pinned to the verified public destination address.")
            elif is_allowed_origin(url):
                origin = f"{parsed.scheme}://{parsed.hostname.lower()}" + (f":{parsed.port}" if parsed.port else "")
            else:
                raise ValueError("Only explicitly approved HTTPS origins and local demo origins can be opened.")
        except (UnsafeDestination, ValueError) as exc:
            raise ValueError(str(exc)) from exc
        if origin not in self._approved_origins:
            raise ValueError("Destination origin has not been explicitly approved.")

        page = await self.get_page()
        async with self._lock:
            self._navigation_origin = origin
            self._block_page_egress = False
            self._allow_document_navigation = True
            self._expected_document_url = url
            self._initial_document_seen = False
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
            finally:
                self._allow_document_navigation = False
            self._current_url = page.url
            final = urlsplit(self._current_url)
            final_origin = f"{final.scheme}://{final.hostname}" + (f":{final.port}" if final.port else "")
            if final_origin.lower() != origin:
                raise ValueError("Navigation redirected outside the approved destination origin.")
            try:
                self._latest_screenshot = await page.screenshot(type="png", full_page=False)
            except Exception as err:
                logger.warning("Could not capture screenshot on navigate: %s", err)

            status_code = response.status if response else 200
            return {
                "ok": True,
                "url": self._current_url,
                "status_code": status_code,
            }

    async def _guard_request(self, route) -> None:
        """Keep all browser egress on the explicitly approved public HTTPS origin."""
        try:
            request_url = route.request.url
            parsed = urlsplit(request_url)
            origin = f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
            if self._navigation_origin is None or origin.lower() != self._navigation_origin:
                raise ValueError("Cross-origin browser requests are blocked.")
            if route.request.method.upper() not in {"GET", "HEAD"}:
                raise ValueError("Browser write requests are blocked; CarryFlow does not submit applications.")
            if self._block_page_egress:
                raise ValueError("Page network requests are blocked while approved values are being prepared.")
            if route.request.is_navigation_request() and route.request.resource_type == "document":
                if not self._allow_document_navigation:
                    raise ValueError("Page-initiated navigation and form submission are blocked.")
                if route.request.redirected_from is None:
                    if self._initial_document_seen or request_url != self._expected_document_url:
                        raise ValueError("Page-initiated navigation and form submission are blocked.")
                    self._initial_document_seen = True
            if parsed.scheme == "https":
                await asyncio.to_thread(validate_public_https_url, request_url)
            elif not is_allowed_origin(request_url):
                raise ValueError("Unsupported browser request scheme.")
            await route.continue_()
        except Exception as exc:
            logger.info("Blocked browser request: %s", exc)
            await route.abort("blockedbyclient")

    async def capture_screenshot(self) -> bytes:
        """Capture screenshot of the current page."""
        page = await self.get_page()
        async with self._lock:
            screenshot = await page.screenshot(type="png", full_page=False)
            self._latest_screenshot = screenshot
            return screenshot

    def get_status(self) -> dict[str, Any]:
        """Return current status of Playwright automation."""
        return {
            "running": self.is_running,
            "ready": self.is_ready,
            "current_url": self._current_url,
            "has_screenshot": self._latest_screenshot is not None,
            "error": self._error,
        }


# Global singleton instance
_manager_instance: PlaywrightManager | None = None


def get_playwright_manager() -> PlaywrightManager:
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = PlaywrightManager()
    return _manager_instance
