"""tiket.com automation plugin.

Implements the realistic ticket-purchase pipeline shown across the tiket.com
events catalogue:

    1. Login (optional, uses ``profile.login`` credentials)
    2. Search the events catalogue for a keyword
    3. Open the matching event (substring match against listed cards)
    4. Open the "Pesan tiket" / package selection screen
    5. Pick a category (FESTIVAL, CAT 1, ...) when supplied
    6. Pick a package row by name (e.g. "CAT 1 RIGHT - GENERAL SALE") and click "Pilih"
    7. Set the desired quantity using the (+) / (-) controls and click "Pesan"
    8. Fill the buyer details ("Detail Pemesanan") and any attendee details
       ("Detail Pengunjung") using the autofill engine + profile attendees
    9. Stop one click short of payment - the operator confirms manually

CAPTCHA, OTP, queue-it screens, and any other anti-bot challenges are detected
and handed back to the operator. Nothing is bypassed.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import quote_plus

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeout
from rapidfuzz import fuzz

from mansautomation.automation.human_intervention import HumanInterventionDetector
from mansautomation.automation.presale import SaleStatus, SaleStatusDetector
from mansautomation.automation.queue_wait import QueueDetector, QueueStatus
from mansautomation.core.exceptions import HumanInterventionRequired
from mansautomation.core.models import WorkflowEventLevel
from mansautomation.plugins.base import (
    AutomationPlugin,
    PluginContext,
    PluginExecutionResult,
    PluginMetadata,
)

_HOME_URL = "https://www.tiket.com/"
_EVENTS_SEARCH_URL = "https://www.tiket.com/id-id/to-do/search?title={query}&utm_source=INTERNAL&utm_medium=home&productAllCategoryCodes=EVENT"
_LOGIN_URL = "https://www.tiket.com/login"

_OTP_KEYWORDS = (
    "verifikasi",
    "kode otp",
    "kode verifikasi",
    "verification code",
    "one-time password",
)

# Shared JS that tags and returns every leaf package card. Works against the
# main page or any child frame. A *leaf* card is the tightest ancestor of a
# single Pilih/Select button.
_CARD_ENUMERATION_JS = r"""() => {
    document.querySelectorAll('[data-mans-package-card]').forEach(
        (el) => el.removeAttribute('data-mans-package-card')
    );
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const isPilih = (b) => /\bPilih\b|\bSelect\b|\bBeli\b/i.test(norm(b.innerText));
    const countPilih = (el) => [
        ...el.querySelectorAll('button'),
        ...el.querySelectorAll('[role="button"]'),
        ...el.querySelectorAll('a'),
    ].filter(isPilih).length;

    const out = [];
    const clickable = [
        ...document.querySelectorAll('button'),
        ...document.querySelectorAll('[role="button"]'),
        ...document.querySelectorAll('a'),
    ];
    const pilihButtons = clickable.filter(isPilih);
    let counter = 0;
    const seen = new Set();

    for (const btn of pilihButtons) {
        // Climb UP collecting the LARGEST ancestor that still contains
        // exactly ONE Pilih button. That ancestor is the full package card
        // (title + price + action), whereas the tightest ancestor would be
        // just the price/footer row.
        let node = btn.parentElement;
        let card = null;
        for (let d = 0; d < 14 && node && node !== document.body; d++) {
            const n = countPilih(node);
            if (n === 1) {
                const text = norm(node.innerText);
                if (text.length > 0 && text.length < 6000) {
                    card = node;          // keep climbing - prefer larger
                }
            } else if (n > 1) {
                break;                    // climbed past the card boundary
            }
            node = node.parentElement;
        }
        if (!card) card = btn.parentElement;
        if (!card || card === document.body) continue;
        if (seen.has(card)) continue;
        seen.add(card);

        card.setAttribute('data-mans-package-card', String(counter));

        // Title: first heading-like element whose text is NOT a price/label.
        let title = '';
        for (const sel of ['h1','h2','h3','h4','h5','h6','strong','b',
                           '[class*="title" i]','[class*="name" i]','[class*="label" i]']) {
            const els = card.querySelectorAll(sel);
            for (const el of els) {
                const t = norm(el.innerText);
                if (t && t.length > 3 &&
                    !/^(Rp|IDR|\d[\d.,]*|Pilih|Select|Beli|Detail|Tidak|Berdiri|Seluruh)/i.test(t)) {
                    title = t; break;
                }
            }
            if (title) break;
        }
        // Fallback: first text line in the card that looks like a name.
        if (!title) {
            const ls = norm(card.innerText).split('\n').map(s => s.trim())
                .filter(l => l && l.length > 3 &&
                    !/^(Rp|IDR|\d[\d.,]*|Pilih|Select|Beli|Detail|Tidak|Berdiri|Seluruh)/i.test(l));
            title = ls[0] || '';
        }
        // Last resort: the whole first line (so we still have *something*).
        if (!title) {
            title = norm(card.innerText).split('\n')[0] || '';
        }

        out.push({
            index: counter,
            title: title,
            text: norm(card.innerText).slice(0, 600),
        });
        counter++;
    }
    return out;
}"""


class TiketComPlugin(AutomationPlugin):
    metadata = PluginMetadata(
        id="tiket.com",
        name="Tiket.com",
        version="2.0.0",
        description=(
            "Sign in, search events, open a chosen event, pick a package and quantity, "
            "and pre-fill buyer + attendee details on tiket.com."
        ),
        author="MansAutomation",
        target_domains=("tiket.com",),
        capabilities=(
            "login",
            "search",
            "event-discovery",
            "ticket-booking",
            "human-intervention",
        ),
    )

    # Set when packages are found inside an iframe so subsequent card clicks
    # can be scoped to that frame instead of the main page.
    _package_frame: Any = None

    async def execute(self, context: PluginContext) -> PluginExecutionResult:
        page = context.page
        if page is None:
            return PluginExecutionResult(success=False, message="page was not initialised")

        params = context.job.parameters or {}
        action = str(params.get("action", "search")).lower()
        query = str(params.get("search_query", "")).strip()
        wants_login = bool(params.get("login", True))

        await context.emit_event(
            "starting tiket.com workflow",
            context={"action": action, "search_query": query, "login": wants_login},
        )

        result_data: dict[str, Any] = {}

        # ── Login decision ──────────────────────────────────────────────
        # Always probe the current session first. The "sign in" toggle only
        # matters when the session is NOT already authenticated:
        #
        #   already signed in            -> proceed (skip login entirely)
        #   anonymous + sign-in ON       -> log in (auto if creds, else manual)
        #   anonymous + sign-in OFF      -> abort with guidance to enable it
        already_signed_in = await self._probe_existing_session(page, context)
        if already_signed_in:
            result_data["logged_in"] = True
        elif wants_login:
            if self._has_credentials(context):
                result_data["logged_in"] = await self._login(page, context)
            else:
                await context.emit_event(
                    "sign-in requested but no credentials on profile - manual login",
                    level=WorkflowEventLevel.WARN,
                )
                try:
                    await page.goto(_LOGIN_URL, wait_until="domcontentloaded")
                except Exception:  # noqa: BLE001
                    pass
                result_data["logged_in"] = await self._request_manual_login(
                    page, context, "no login credentials configured on profile"
                )
        else:
            # Anonymous + sign-in toggle OFF. Do not touch the login page;
            # abort with a clear instruction instead.
            result_data["logged_in"] = False
            return PluginExecutionResult(
                success=False,
                message=(
                    "You are not signed in to tiket.com and the 'Sign in' option "
                    "is turned off. Enable 'Sign in' (and set your login email/"
                    "password on the profile) to use the login feature."
                ),
                data=result_data,
            )

        if action == "login":
            return PluginExecutionResult(
                success=bool(result_data["logged_in"]),
                message="login completed" if result_data["logged_in"] else "login skipped",
                data=result_data,
            )

        if action == "search":
            if not query:
                return PluginExecutionResult(
                    success=False,
                    message="search_query parameter is required for the search action",
                    data=result_data,
                )
            events = await self._search_events(page, context, query)
            result_data.update({"query": query, "events": events, "event_count": len(events)})
            return PluginExecutionResult(
                success=True,
                message=f"found {len(events)} event(s) for '{query}'",
                data=result_data,
            )

        if action == "book":
            return await self._book(page, context, result_data, params)

        return PluginExecutionResult(
            success=False,
            message=f"unsupported action: {action}",
            data=result_data,
        )

    # ------------------------------------------------------------------ login

    @staticmethod
    def _has_credentials(context: PluginContext) -> bool:
        login = context.profile.login
        return bool(login.email and login.password_value())

    async def _login(self, page: Page, context: PluginContext) -> bool:
        # Defensive: if the persistent context already has a session (e.g.
        # cookies survived a previous run), don't touch the login UI.
        if await self._is_signed_in(page):
            await context.emit_event("already signed in - skipping automatic login")
            return True

        await context.emit_event("opening tiket.com login page")
        try:
            await page.goto(_LOGIN_URL, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            await context.emit_event(
                f"failed to open login page: {exc}",
                level=WorkflowEventLevel.WARN,
            )
            return await self._request_manual_login(page, context, "could not open login page")

        await context.sync.wait_for_page_ready(page)

        # /login redirects to the homepage when an authenticated session
        # already exists - treat that as success and stop poking forms.
        current = page.url or ""
        if (
            "/login" not in current
            and "account.bliblitiket" not in current
            and await self._is_signed_in(page)
        ):
            await context.emit_event(
                "login page redirected away - session is already authenticated",
                context={"url": current},
            )
            return True

        await self._await_human_when_needed(page, context)

        # The new tiket.com sign-in lives on account.bliblitiket.com and asks
        # the user to choose between Google / Phone or Email / Apple etc. We
        # always click "Continue with Phone or Email" if it shows up.
        continue_with_email = (
            "button:has-text('Continue with Phone or Email')",
            "button:has-text('Lanjut dengan No. HP atau Email')",
            "button:has-text('Lanjutkan dengan No. HP atau Email')",
            "button:has-text('Lanjutkan dengan Email')",
            "[data-testid*='phone-or-email' i]",
            "[data-testid*='emailOrPhone' i]",
        )
        await context.sync.resilient_click(
            page, continue_with_email, max_attempts=3, timeout_ms=6_000,
        )
        await context.sync.wait_for_dom_settle(page, quiet_ms=300, timeout_ms=4_000)
        await self._await_human_when_needed(page, context)

        # === Step 1: email field + Selanjutnya/Continue ===
        email_filled = await self._fill_email_step(page, context)
        if not email_filled:
            return await self._request_manual_login(
                page, context, "email field not found on tiket.com login page"
            )

        await self._click_email_continue(page, context)

        # === Step 2: wait for the password field to render, fill it, submit ===
        password_field = await self._wait_for_password_field(page, context)
        if password_field is None:
            return await self._request_manual_login(
                page, context, "password field never appeared after email step"
            )

        if not await self._fill_password_step(page, context, password_field):
            return await self._request_manual_login(
                page, context, "could not fill password field automatically"
            )
        await self._click_login_submit(page, context)

        await self._await_human_when_needed(page, context, extra_keywords=_OTP_KEYWORDS)
        await context.sync.wait_for_network_quiet(page, timeout_ms=15_000)

        signed_in = await self._is_signed_in(page)
        if not signed_in:
            return await self._request_manual_login(
                page, context, "automatic login finished but session is not authenticated"
            )
        await context.emit_event("login finished", context={"signed_in": True, "url": page.url})
        return True

    async def _request_manual_login(
        self, page: Page, context: PluginContext, reason: str
    ) -> bool:
        """Fall back to manual login. Pauses the workflow, opens the login
        page so the user can sign in interactively, then verifies the session
        before continuing."""

        await context.emit_event(
            f"falling back to manual login: {reason}",
            level=WorkflowEventLevel.WARN,
        )
        # Make sure the user is looking at the login screen.
        try:
            if "/login" not in (page.url or "") and "account.bliblitiket" not in (page.url or ""):
                await page.goto(_LOGIN_URL, wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001
            pass
        try:
            await page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass

        await context.request_human(
            "Manual login required on tiket.com. Sign in inside the browser window, "
            f"then click 'I'm done - resume'. Reason: {reason}",
            url=page.url,
        )
        if context.is_aborted():
            raise HumanInterventionRequired(reason, url=page.url)

        try:
            await context.sync.wait_for_network_quiet(page, timeout_ms=10_000)
        except Exception:  # noqa: BLE001
            pass
        signed_in = await self._is_signed_in(page)
        await context.emit_event(
            "resumed after manual login",
            context={"signed_in": signed_in, "url": page.url},
        )
        return signed_in

    # ------------------------------------------------------- login: email step

    async def _fill_email_step(self, page: Page, context: PluginContext) -> bool:
        """Fill ONLY the email/phone field, regardless of what else is on the
        page. tiket.com renders an email-first sign-in: we never type the
        password until the next screen appears."""

        email = context.profile.login.email
        if not email:
            return False
        result = await context.sync.resilient_fill(
            page,
            (
                "input[type='email']",
                "input[name='email']",
                "input[name='username']",
                "input[name='emailOrPhone']",
                "input[id*='email' i]",
                "input[autocomplete='username']",
                "input[autocomplete='email']",
                "input[placeholder*='Email' i]",
                "input[placeholder*='No. HP' i]",
                "input[placeholder*='Phone' i]",
            ),
            email,
            max_attempts=3,
            timeout_ms=6_000,
            typing_delay_ms=context.workflow_settings.field_typing_delay_ms,
        )
        if result.success:
            await context.emit_event(
                "filled login email", context={"selector": result.selector}
            )
        return result.success

    async def _click_email_continue(self, page: Page, context: PluginContext) -> None:
        """Click 'Selanjutnya' / 'Continue' / 'Next' to advance to the password
        screen. The button is often disabled until the email passes client-side
        validation, so we wait for it to become enabled first."""

        await context.sync.click_when_enabled(
            page,
            (
                "button:has-text('Selanjutnya')",
                "button:has-text('Lanjutkan')",
                "button:has-text('Lanjut')",
                "button:has-text('Continue')",
                "button:has-text('Next')",
                "button[data-testid*='continue' i]",
                "button[data-testid*='next' i]",
                "button[type='submit']",
            ),
            enable_timeout_ms=10_000,
            click_timeout_ms=4_000,
        )
        # Give the password screen a chance to mount before the next probe.
        await context.sync.wait_for_dom_settle(page, quiet_ms=350, timeout_ms=4_000)

    # ---------------------------------------------------- login: password step

    async def _wait_for_password_field(
        self, page: Page, context: PluginContext
    ) -> str | None:
        """Wait for the password field to render after the email step. tiket.com
        does this client-side, so we cannot rely on a navigation event."""

        password_selectors = (
            "input[type='password']",
            "input[name='password']",
            "input[autocomplete='current-password']",
            "input[autocomplete='new-password']",
            "input[id*='password' i]",
        )
        selector = await context.sync.wait_for_any(
            page, password_selectors, timeout_ms=15_000
        )
        if selector is None:
            return None
        await context.emit_event(
            "password field appeared", context={"selector": selector}
        )
        return selector

    async def _fill_password_step(
        self, page: Page, context: PluginContext, password_selector: str
    ) -> bool:
        password = context.profile.login.password_value() or ""
        if not password:
            await context.emit_event(
                "no password configured on profile", level=WorkflowEventLevel.WARN
            )
            return False
        result = await context.sync.resilient_fill(
            page,
            (
                password_selector,
                "input[type='password']",
                "input[name='password']",
                "input[autocomplete='current-password']",
            ),
            password,
            max_attempts=3,
            timeout_ms=6_000,
            typing_delay_ms=context.workflow_settings.field_typing_delay_ms,
        )
        if result.success:
            await context.emit_event(
                "filled login password", context={"selector": result.selector}
            )
        return result.success

    async def _click_login_submit(self, page: Page, context: PluginContext) -> None:
        await context.sync.click_when_enabled(
            page,
            (
                "button:has-text('Masuk')",
                "button:has-text('Sign in')",
                "button:has-text('Log in')",
                "button:has-text('Login')",
                "button[data-testid*='login' i]",
                "button[data-testid*='submit' i]",
                "button[type='submit']",
            ),
            enable_timeout_ms=10_000,
            click_timeout_ms=4_000,
        )

    async def _is_signed_in(self, page: Page) -> bool:
        """Determine whether the current tiket.com session is authenticated.

        Detection order (most reliable first):

          1. A visible login affordance ('Masuk' / 'Daftar' / 'Sign in' /
             'Login' / a link to /login) => DEFINITELY anonymous.
          2. A profile-menu / account / avatar element, OR a strong auth
             cookie => signed in.
          3. Header present but no login affordance => signed in (tiket.com
             always shows a login button to anonymous visitors).

        Combining the negative and positive signals avoids both the
        false-positive (skipping login when anonymous) and the false-negative
        (sending an authenticated user to the login page).
        """

        # 1) Login affordance => anonymous.  Checked first and thoroughly.
        login_affordances = (
            "header a[href*='/login' i]",
            "a[href*='account.bliblitiket' i]",
            "header button:has-text('Masuk')",
            "header button:has-text('Daftar')",
            "header a:has-text('Masuk')",
            "header a:has-text('Daftar')",
            "header :text-is('Masuk')",
            "header :text-is('Sign in')",
            "header :text-is('Log in')",
            "[data-testid*='loginButton' i]",
            "[data-testid*='signInButton' i]",
            "[data-testid*='registerButton' i]",
        )
        for selector in login_affordances:
            try:
                if await page.locator(selector).first.is_visible(timeout=400):
                    return False
            except Exception:  # noqa: BLE001
                continue

        # 2a) Positive DOM signals.
        positive_selectors = (
            "[data-testid='headerProfileMenu']",
            "[data-testid='profileMenu']",
            "[data-testid='headerUserName']",
            "[data-testid*='userName' i]",
            "[data-testid*='avatar' i]",
            "[class*='profileMenu' i]",
            "[class*='ProfileMenu']",
            "a[href*='/myorder' i]",
            "a[href*='/pesanan' i]",
            "a[href*='/myaccount' i]",
            "a[href*='/akun' i]",
            "img[alt*='profile' i]",
            "img[alt*='avatar' i]",
            "header :text('Keluar')",
            "header :text('Logout')",
        )
        for selector in positive_selectors:
            try:
                if await page.locator(selector).first.is_visible(timeout=400):
                    return True
            except Exception:  # noqa: BLE001
                continue

        # 2b) Strong auth-cookie probe (exact names only).
        try:
            cookies = await page.context.cookies()
        except Exception:  # noqa: BLE001
            cookies = []
        strong_auth_cookies = {
            "ssotkn",
            "accesstoken",
            "access_token",
            "blibliticket-jwt",
            "tiket_session",
            "tiket-session-id",
            "islogin",
            "is_login",
        }
        for cookie in cookies:
            try:
                name = str(cookie.get("name", "")).lower()
                value = str(cookie.get("value", "")).strip()
            except Exception:  # noqa: BLE001
                continue
            if not value or value.lower() in {"false", "0", "null"}:
                continue
            if name in strong_auth_cookies:
                return True

        # 3) Header present but no login affordance => signed in.
        try:
            if await page.locator("header").first.is_visible(timeout=400):
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    async def _probe_existing_session(self, page: Page, context: PluginContext) -> bool:
        """Detect an already-authenticated session before touching the login
        flow. Avoids the 'already logged in' regression where /login redirects
        away and the email field never renders.

        The runner has already navigated to the workflow's target URL and run
        ``wait_for_page_ready`` for us, so we don't repeat that work here.
        We only navigate to the homepage when the page is still on
        ``about:blank``.
        """

        try:
            current = page.url or ""
            if not current or current == "about:blank":
                await page.goto(_HOME_URL, wait_until="domcontentloaded")
                # Only wait for hydration here - the runner already did one
                # for the navigated target URL, so skip a second full
                # ``wait_for_page_ready`` round.
                await context.sync.wait_for_dom_settle(
                    page, quiet_ms=300, timeout_ms=3_000
                )
        except Exception as exc:  # noqa: BLE001
            await context.emit_event(
                f"session probe navigation failed: {exc}",
                level=WorkflowEventLevel.WARN,
            )
            return False
        signed_in = await self._is_signed_in(page)
        if signed_in:
            await context.emit_event(
                "existing tiket.com session detected - skipping login",
                context={"url": page.url},
            )
        return signed_in

    # ----------------------------------------------------------------- search

    async def _search_events(
        self,
        page: Page,
        context: PluginContext,
        query: str,
    ) -> list[dict[str, str]]:
        target = _EVENTS_SEARCH_URL.format(query=quote_plus(query))
        await context.emit_event("navigating to events search", context={"url": target})
        nav_ok = True
        try:
            await page.goto(target, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            nav_ok = False
            await context.emit_event(
                f"events search navigation failed, falling back to UI search: {exc}",
                level=WorkflowEventLevel.WARN,
            )

        await self._await_human_when_needed(page, context)

        if nav_ok:
            # Wait for an actual event card anchor to render. Avoid
            # ``wait_for_load_state('networkidle')`` here: tiket.com keeps
            # websockets / heartbeats alive so the event never fires and the
            # whole search slows by ~25s. The card selector resolves in well
            # under 5s on a healthy connection.
            speed = max(0.25, float(getattr(context.workflow_settings, "sync_speed_multiplier", 1.0)))
            try:
                await page.wait_for_selector(
                    "a[href*='/to-do/'], a[href*='/event/']",
                    state="visible",
                    timeout=int(8_000 * speed),
                )
            except Exception:  # noqa: BLE001 - empty results page is also legal
                pass
            await context.sync.wait_for_dom_settle(
                page, quiet_ms=int(250 * speed), timeout_ms=int(2_500 * speed)
            )
            events = await self._collect_event_cards(page)
            if events:
                await context.emit_event(
                    "search completed",
                    context={"results": len(events), "url": page.url},
                )
                return events

        # Fallback: only reached when the search URL didn't render any cards
        # (e.g. tiket.com changed its URL contract or the result page renders
        # via the home-page search bar).
        await page.goto(_HOME_URL, wait_until="domcontentloaded")
        await self._submit_search_via_ui(page, context, query)
        try:
            await page.wait_for_selector(
                "a[href*='/to-do/'], a[href*='/event/']",
                state="visible",
                timeout=10_000,
            )
        except Exception:  # noqa: BLE001
            pass
        events = await self._collect_event_cards(page)
        await context.emit_event(
            "search completed",
            context={"results": len(events), "url": page.url, "via": "ui_fallback"},
        )
        return events

    async def _submit_search_via_ui(
        self,
        page: Page,
        context: PluginContext,
        query: str,
    ) -> None:
        candidates = (
            "input[placeholder*='Cari aktivitas' i]",
            "input[placeholder*='Cari' i]",
            "input[placeholder*='Search' i]",
            "input[type='search']",
            "input[aria-label*='search' i]",
        )
        for selector in candidates:
            try:
                locator = page.locator(selector).first
                if not await locator.is_visible(timeout=1_500):
                    continue
                await locator.click()
                await locator.fill("")
                await locator.type(query, delay=20)
                await locator.press("Enter")
                await context.emit_event(
                    "submitted search via UI",
                    context={"selector": selector, "query": query},
                )
                return
            except Exception:  # noqa: BLE001
                continue
        await context.emit_event(
            "could not locate a search input on the page",
            level=WorkflowEventLevel.WARN,
        )

    async def _collect_event_cards(self, page: Page) -> list[dict[str, str]]:
        try:
            payload = await page.evaluate(
                """() => {
                    const anchors = Array.from(document.querySelectorAll("a[href*='/to-do/'], a[href*='/event/']"));
                    const out = [];
                    const seen = new Set();
                    for (const a of anchors) {
                        const href = a.href;
                        if (!href || seen.has(href)) continue;
                        if (href.includes('/search')) continue;
                        const text = (a.innerText || '').trim();
                        if (!text || text.length < 4) continue;
                        seen.add(href);
                        out.push({
                            title: text.split('\\n')[0].slice(0, 200),
                            url: href,
                            description: text.slice(0, 400),
                        });
                        if (out.length >= 25) break;
                    }
                    return out;
                }"""
            )
        except Exception:
            return []
        if not isinstance(payload, list):
            return []
        results: list[dict[str, str]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            url = str(item.get("url", "")).strip()
            if title and url:
                results.append({
                    "title": title,
                    "url": url,
                    "description": str(item.get("description", "")).strip(),
                })
        return results

    # ----------------------------------------------------------- booking flow

    async def _book(
        self,
        page: Page,
        context: PluginContext,
        result_data: dict[str, Any],
        params: dict[str, Any],
    ) -> PluginExecutionResult:
        event_title = str(params.get("event_title") or "").strip()
        # Accept both single-value and list-based parameters. The GUI sends
        # comma-separated lists; older callers may still send a single string.
        packages = self._coerce_string_list(
            params.get("packages") or params.get("package")
        )
        categories = self._coerce_string_list(
            params.get("categories") or params.get("category")
        )
        quantity = int(params.get("quantity") or 1)
        query = str(params.get("search_query") or event_title).strip()

        if not query and not event_title:
            return PluginExecutionResult(
                success=False,
                message="event_title or search_query is required for booking",
                data=result_data,
            )
        if not packages:
            return PluginExecutionResult(
                success=False,
                message="at least one package is required for booking",
                data=result_data,
            )

        events = await self._search_events(page, context, query or event_title)
        result_data["search_results"] = len(events)
        target = self._best_match(events, event_title or query)
        if not target:
            return PluginExecutionResult(
                success=False,
                message=f"no matching event found for '{event_title or query}'",
                data=result_data,
            )
        result_data["event"] = target

        await context.emit_event("opening event page", context={"url": target["url"]})
        await page.goto(target["url"], wait_until="domcontentloaded")
        await self._await_human_when_needed(page, context)
        # Wait for the hero CTA to render rather than gambling on networkidle.
        speed = max(0.25, float(getattr(context.workflow_settings, "sync_speed_multiplier", 1.0)))
        try:
            await page.wait_for_selector(
                "h1, h2",
                state="visible",
                timeout=int(6_000 * speed),
            )
        except Exception:  # noqa: BLE001
            pass
        await context.sync.wait_for_dom_settle(
            page, quiet_ms=int(300 * speed), timeout_ms=int(3_000 * speed)
        )

        # Honor a pre-sale countdown / "tickets not yet available" state.
        status = await self._handle_presale(page, context, params, result_data)
        if not status.is_open:
            return PluginExecutionResult(
                success=False,
                message=status.detail or "tickets are not yet on sale",
                data=result_data,
            )

        if not await self._click_buy_ticket_cta(page, context):
            raise HumanInterventionRequired(
                "could not click the 'Beli tiket sekarang' / buy CTA",
                url=page.url,
            )

        # If a waiting room / queue appears after clicking buy, actively wait
        # in line — tracking position — until it releases us. This must run
        # BEFORE we look for packages, since the package list only renders
        # once the queue clears.
        await self._wait_through_queue(page, context, params, result_data)

        # The buy CTA navigates to the /packages sub-page.  Wait for that
        # navigation to settle before we start looking for Pilih buttons.
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=int(12_000 * speed))
        except Exception:  # noqa: BLE001
            pass
        # Give React/Next.js time to hydrate the package list before we probe.
        await context.sync.wait_for_hydration(page, timeout_ms=int(8_000 * speed))
        await context.sync.wait_for_dom_settle(
            page, quiet_ms=int(300 * speed), timeout_ms=int(4_000 * speed)
        )
        # Hard-wait for any known package-page landmark.
        package_landmarks = (
            # The "Paket" / "Packages" section heading
            "text=Paket",
            "text=Packages",
            # Pricing text always present on the packages page
            "[class*='price' i]",
            "[class*='Price']",
            # Heading pattern inside the category section (e.g. "FESTIVAL")
            "h2",
        )
        for landmark in package_landmarks:
            try:
                if await page.locator(landmark).first.is_visible(timeout=int(3_000 * speed)):
                    break
            except Exception:  # noqa: BLE001
                continue
        await self._await_human_when_needed(page, context)

        # Try the package fallback chain: each package x each category combo.
        # If categories is empty we still attempt every package once on the
        # currently visible category list.
        chosen = await self._select_package_with_fallback(
            page, context, packages, categories
        )
        if chosen is None:
            return PluginExecutionResult(
                success=False,
                message=(
                    f"none of the requested packages {packages} were available "
                    f"in categories {categories or '<any>'}"
                ),
                data=result_data,
            )
        result_data["package_chosen"] = chosen["package"]
        if chosen.get("category"):
            result_data["category_chosen"] = chosen["category"]

        await self._set_quantity(page, context, quantity)
        await self._click_pesan_button(page, context)

        # The order/buyer-details page has Detail Pemesanan as a heading.
        try:
            await page.wait_for_selector(
                "text=Detail Pemesanan, text=Order Details",
                state="visible",
                timeout=int(12_000 * speed),
            )
        except Exception:  # noqa: BLE001
            pass
        await self._await_human_when_needed(page, context)

        await self._fill_order_details(page, context)
        await self._fill_visitor_details(page, context, quantity)

        # Stop one click short of payment so the user reviews + confirms.
        await context.request_human(
            "Booking form filled. Review the order details and click 'Lanjutkan pembayaran' to proceed with payment.",
            url=page.url,
        )
        result_data["url"] = page.url
        result_data["packages_requested"] = packages
        result_data["categories_requested"] = categories
        result_data["quantity"] = quantity
        return PluginExecutionResult(
            success=True,
            message="booking form filled - manual confirmation required",
            data=result_data,
        )

    @staticmethod
    def _coerce_string_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [piece.strip() for piece in value.split(",") if piece.strip()]
        if isinstance(value, (list, tuple)):
            return [str(piece).strip() for piece in value if str(piece).strip()]
        return []

    async def _handle_presale(
        self,
        page: Page,
        context: PluginContext,
        params: dict[str, Any],
        result_data: dict[str, Any],
    ) -> SaleStatus:
        """Detect a pre-sale countdown and either wait it out or notify.

        Behaviour (controlled by job params):
          - ``presale_wait``            bool, default True
          - ``presale_max_wait_minutes`` int, default 30

        When ``presale_wait`` is on AND a concrete open time / countdown was
        parsed, we wait the FULL duration (capped at a 24h safety limit),
        regardless of the minute budget, then auto-resume the moment the buy
        button becomes clickable. The minute budget only governs the fallback
        case where the countdown can't be parsed.
        """

        detector = SaleStatusDetector()
        status = await detector.detect(page)
        if status.is_open:
            return status

        wait_enabled = bool(params.get("presale_wait", True))
        max_wait_minutes = max(0, int(params.get("presale_max_wait_minutes", 30)))
        hard_cap_seconds = 24 * 3600  # never wait longer than a day

        await context.emit_event(
            f"ticket sale not open yet - {status.detail}",
            level=WorkflowEventLevel.WARN,
            context={
                "detail": status.detail,
                "seconds_until_open": status.seconds_until_open,
                "starts_at": status.starts_at,
            },
        )
        result_data["presale"] = {
            "detail": status.detail,
            "seconds_until_open": status.seconds_until_open,
            "starts_at": status.starts_at,
            "snippet": status.snippet,
        }

        if not wait_enabled:
            await context.request_human(
                f"Tickets are not yet on sale. {status.detail}. "
                "Enable 'Pre-sale auto-wait' to wait automatically, or resume "
                "manually once the sale opens.",
                url=page.url,
            )
            return await detector.detect(page)

        seconds = status.seconds_until_open
        if seconds is None:
            # Unknown countdown: fall back to the minute budget, then ask.
            budget = max_wait_minutes * 60
            if budget <= 0:
                await context.request_human(
                    f"Tickets not on sale and the countdown is unreadable. "
                    f"{status.detail}. Resume when ready.",
                    url=page.url,
                )
                return await detector.detect(page)
            return await self._wait_for_sale_to_open(page, context, status, budget)

        if seconds > hard_cap_seconds:
            await context.request_human(
                f"Tickets open in {self._format_duration(seconds)} ({status.detail}) - "
                "that is more than 24 hours away. Resume closer to the sale time.",
                url=page.url,
            )
            return await detector.detect(page)

        await context.emit_event(
            f"auto-waiting {self._format_duration(seconds)} until sale opens "
            f"({status.detail})",
        )
        # Wait the full parsed duration (+ a 2-minute safety tail to absorb
        # countdown drift) and auto-resume when the buy button is ready.
        return await self._wait_for_sale_to_open(
            page, context, status, seconds + 120
        )

    async def _wait_for_sale_to_open(
        self,
        page: Page,
        context: PluginContext,
        status: SaleStatus,
        budget_seconds: int,
    ) -> SaleStatus:
        """Efficiently wait until the sale opens, then auto-resume.

        Two phases:
          - FAR  (> 90s remaining): sleep in coarse chunks and reload the page
            every ~2 minutes so the client-side countdown stays fresh. Emits a
            progress line about once a minute. Low CPU, keeps the tab alive.
          - NEAR (<= 90s remaining): reload once, then poll every second for
            the buy button to flip from the disabled "Beli tiket dalam ..."
            state to an enabled CTA. Returns the instant it is clickable so
            the caller clicks it immediately at the exact open moment.
        """

        detector = SaleStatusDetector()
        loop = asyncio.get_event_loop()
        start = loop.time()
        deadline = start + max(5, budget_seconds)
        last_progress = 0.0
        last_reload = start

        while loop.time() < deadline:
            if context.is_aborted():
                raise HumanInterventionRequired("workflow aborted while waiting for sale")

            current = await detector.detect(page)

            # Open signals: detector says open OR the buy button is clickable.
            if current.is_open or await self._is_buy_button_ready(page):
                await context.emit_event("sale is now open - resuming automatically")
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=8_000)
                except Exception:  # noqa: BLE001
                    pass
                return SaleStatus(is_open=True, detail="sale opened", url=page.url)

            remaining = current.seconds_until_open
            now = loop.time()

            if remaining is not None and remaining <= 90:
                # NEAR phase: reload once to surface the live CTA, then poll
                # tightly until the button is ready.
                try:
                    await page.reload(wait_until="domcontentloaded")
                    await context.sync.wait_for_dom_settle(page, quiet_ms=200, timeout_ms=2_000)
                except Exception:  # noqa: BLE001
                    pass
                tight_deadline = now + max(remaining + 30, 60)
                await context.emit_event(
                    f"sale opens in ~{self._format_duration(remaining)} - polling for the buy button"
                )
                while loop.time() < tight_deadline:
                    if context.is_aborted():
                        raise HumanInterventionRequired("workflow aborted while waiting for sale")
                    if await self._is_buy_button_ready(page):
                        await context.emit_event("buy button is live - resuming")
                        return SaleStatus(is_open=True, detail="sale opened", url=page.url)
                    refreshed = await detector.detect(page)
                    if refreshed.is_open:
                        return SaleStatus(is_open=True, detail="sale opened", url=page.url)
                    await asyncio.sleep(1)
                    # Reload every ~10s in the tight phase so a stale countdown
                    # widget can't keep the page disabled past the open time.
                    if int(loop.time()) % 10 == 0:
                        try:
                            await page.reload(wait_until="domcontentloaded")
                        except Exception:  # noqa: BLE001
                            pass
                # Tight window elapsed without the button going live - loop
                # again (a fresh detect will recompute remaining).
                continue

            # FAR phase: progress heartbeat ~ once a minute.
            if (now - last_progress) >= 60:
                last_progress = now
                shown = remaining if remaining is not None else int(deadline - now)
                await context.emit_event(
                    f"waiting for sale to open - ~{self._format_duration(int(shown))} left"
                    + (f" ({current.starts_at})" if current.starts_at else "")
                )

            # Coarse sleep, sized to the remaining time but capped.
            sleep_s = 30
            if remaining is not None:
                sleep_s = max(10, min(60, remaining // 4))
            await asyncio.sleep(sleep_s)

            # Reload every ~2 minutes during the far phase to refresh the
            # client-side countdown / session.
            if (loop.time() - last_reload) >= 120:
                last_reload = loop.time()
                try:
                    await page.reload(wait_until="domcontentloaded")
                    await context.sync.wait_for_dom_settle(page, quiet_ms=250, timeout_ms=2_500)
                except Exception:  # noqa: BLE001
                    pass

        # Budget exhausted - final check then hand back to the operator.
        final = await detector.detect(page)
        if final.is_open or await self._is_buy_button_ready(page):
            return SaleStatus(is_open=True, detail="sale opened", url=page.url)
        await context.request_human(
            f"Sale has not opened within the wait window ({final.detail}). "
            "Resume when the buy button is live.",
            url=page.url,
        )
        return await detector.detect(page)

    async def _is_buy_button_ready(self, page: Page) -> bool:
        """Return True when an enabled buy CTA is present.

        Distinguishes the disabled pre-sale countdown button
        ("Beli tiket dalam HH:MM:SS") from the live, clickable
        "Beli tiket sekarang" CTA.
        """

        try:
            return bool(
                await page.evaluate(
                    r"""() => {
                        const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                        const nodes = [
                            ...document.querySelectorAll('button'),
                            ...document.querySelectorAll('[role="button"]'),
                            ...document.querySelectorAll('a'),
                        ];
                        for (const el of nodes) {
                            const t = norm(el.innerText);
                            if (!t) continue;
                            // Must look like a buy CTA.
                            if (!/(beli tiket|buy ticket|pesan tiket|beli sekarang)/.test(t)) continue;
                            // Disabled pre-sale countdown -> not ready.
                            if (/\bdalam\b|\bin\b/.test(t) && /\d{1,2}:\d{2}/.test(t)) continue;
                            if (el.disabled) continue;
                            if (el.getAttribute('aria-disabled') === 'true') continue;
                            // A visible, enabled buy CTA.
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) return true;
                        }
                        return false;
                    }"""
                )
            )
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _format_duration(seconds: int) -> str:
        if seconds <= 0:
            return "now"
        days, remainder = divmod(seconds, 86_400)
        hours, remainder = divmod(remainder, 3_600)
        minutes, secs = divmod(remainder, 60)
        parts: list[str] = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if secs and not days:
            parts.append(f"{secs}s")
        return " ".join(parts) or f"{seconds}s"

    async def _wait_through_queue(
        self,
        page: Page,
        context: PluginContext,
        params: dict[str, Any],
        result_data: dict[str, Any],
    ) -> None:
        """Actively wait inside a waiting room / queue until released.

        Tracks the queue position so the operator can see progress, keeps the
        page alive (no premature close), and returns as soon as the queue
        clears so package selection can proceed. Honors a max-wait budget;
        beyond it, pauses for manual handling rather than failing.

        Parameters (from the job):
          - ``queue_wait``            bool, default True
          - ``queue_max_wait_minutes`` int, default 60
        """

        detector = QueueDetector()
        status = await detector.detect(page)
        if not status.in_queue:
            return

        wait_enabled = bool(params.get("queue_wait", True))
        max_wait_minutes = max(1, int(params.get("queue_max_wait_minutes", 60)))
        max_wait_seconds = max_wait_minutes * 60

        await context.emit_event(
            f"waiting room detected - position={status.position or 'unknown'} "
            f"detail={status.detail[:120]}",
            level=WorkflowEventLevel.WARN,
            context={
                "position": status.position,
                "estimated_wait_seconds": status.estimated_wait_seconds,
                "url": status.url,
            },
        )
        result_data["queue"] = {
            "entered": True,
            "initial_position": status.position,
            "url": status.url,
        }

        if not wait_enabled:
            await context.request_human(
                f"Waiting room detected (position {status.position or 'unknown'}). "
                "Wait for the queue to release, then click 'I'm done - resume'.",
                url=status.url,
            )
            return

        await self._notify_queue(context, status, first=True)

        start = asyncio.get_event_loop().time()
        last_position: int | None = status.position
        last_progress_emit = 0.0
        stagnant_since = start

        while True:
            if context.is_aborted():
                raise HumanInterventionRequired("workflow aborted while in queue")

            elapsed = asyncio.get_event_loop().time() - start
            if elapsed >= max_wait_seconds:
                await context.emit_event(
                    f"queue wait exceeded {max_wait_minutes} min budget - asking operator",
                    level=WorkflowEventLevel.WARN,
                )
                await context.request_human(
                    f"Still in the waiting room after {max_wait_minutes} minutes "
                    f"(position {last_position or 'unknown'}). Resume when released.",
                    url=page.url,
                )
                return

            # Poll the queue state. Keep the tab active by reading the DOM.
            current = await detector.detect(page)
            if not current.in_queue:
                await context.emit_event("queue cleared - proceeding to packages")
                await self._notify_queue(context, current, cleared=True)
                # Let the released page settle before the caller continues.
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                except Exception:  # noqa: BLE001
                    pass
                return

            # Progress tracking.
            now = asyncio.get_event_loop().time()
            pos = current.position
            if pos is not None and pos != last_position:
                last_position = pos
                stagnant_since = now
                if (now - last_progress_emit) >= 5:
                    last_progress_emit = now
                    await context.emit_event(
                        f"queue position: {pos}"
                        + (
                            f" (~{self._format_duration(current.estimated_wait_seconds)} left)"
                            if current.estimated_wait_seconds
                            else ""
                        ),
                        context={"position": pos},
                    )
            elif (now - last_progress_emit) >= 20:
                # Periodic heartbeat even when position text is unparseable.
                last_progress_emit = now
                await context.emit_event(
                    f"still in queue (position {last_position or 'unknown'}, "
                    f"{self._format_duration(int(elapsed))} elapsed)",
                    context={"position": last_position},
                )

            # Detect a frozen/stuck queue (no movement for a long time) and
            # nudge: a light reload can recover a desynced queue-it widget
            # WITHOUT losing the place (queue-it persists position in a cookie).
            if (now - stagnant_since) >= 180:
                stagnant_since = now
                await context.emit_event(
                    "queue appears stuck for 3 min - performing a safe refresh",
                    level=WorkflowEventLevel.WARN,
                )
                try:
                    await page.reload(wait_until="domcontentloaded")
                except Exception:  # noqa: BLE001
                    pass

            await asyncio.sleep(3)

    async def _notify_queue(
        self,
        context: PluginContext,
        status: QueueStatus,
        *,
        first: bool = False,
        cleared: bool = False,
    ) -> None:
        """Emit a queue-state event. The runner mirrors WARNING-level queue
        events to desktop / Telegram / Discord channels."""

        if cleared:
            msg = "Queue cleared - automation is selecting your package now."
        elif first:
            msg = f"You are in the waiting room. Position: {status.position or 'unknown'}."
        else:
            msg = f"Queue position: {status.position or 'unknown'}."
        await context.emit_event(msg, level=WorkflowEventLevel.WARN)

    async def _select_package_with_fallback(
        self,
        page: Page,
        context: PluginContext,
        packages: list[str],
        categories: list[str],
    ) -> dict[str, str] | None:
        """Try every requested package against every visible card."""

        speed = max(0.25, float(getattr(context.workflow_settings, "sync_speed_multiplier", 1.0)))

        # Wait for the initial render of the packages page.
        await self._wait_for_packages(page, context)

        availability = await self._read_category_availability(page)
        if availability:
            await context.emit_event(
                "category availability snapshot",
                context={"availability": availability},
                level=WorkflowEventLevel.DEBUG,
            )

        target_categories = self._expand_categories(categories, availability)
        if categories and target_categories != categories:
            await context.emit_event(
                "category prefixes expanded",
                context={"input": categories, "expanded": target_categories},
                level=WorkflowEventLevel.DEBUG,
            )

        attempted: list[str] = []

        # ── Phase 1 ── Try each category explicitly if user supplied them.
        if target_categories:
            for category in target_categories:
                cat_count = self._lookup_availability(availability, category)
                if cat_count == 0:
                    await context.emit_event(
                        "category sold out per sidebar - skipping",
                        context={"category": category},
                        level=WorkflowEventLevel.WARN,
                    )
                    continue
                await self._select_category(page, context, category)

                # After a category click tiket.com re-renders the visible
                # package list. Poll until the buttons appear — don't rely
                # on a fixed 200ms DOM-settle window which is too short.
                await self._poll_for_pilih(page, context, timeout_ms=int(8_000 * speed))
                await context.sync.wait_for_dom_settle(
                    page, quiet_ms=int(250 * speed), timeout_ms=int(2_000 * speed)
                )

                cards = await self._enumerate_package_cards(page)
                await context.emit_event(
                    "enumerated package cards after category",
                    context={"category": category, "count": len(cards), "titles": [c["title"] for c in cards]},
                    level=WorkflowEventLevel.DEBUG,
                )
                if not cards:
                    continue
                chosen = await self._click_best_available_card(
                    page, context, cards, packages, attempted, category
                )
                if chosen is not None:
                    return chosen

        # ── Phase 2 ── Full-page scan regardless of category.
        # Covers: (a) no sidebar, (b) user left Category blank,
        # (c) all category attempts returned empty.
        await self._poll_for_pilih(page, context, timeout_ms=int(10_000 * speed))
        await context.sync.wait_for_dom_settle(
            page, quiet_ms=int(300 * speed), timeout_ms=int(2_500 * speed)
        )
        all_cards = await self._enumerate_package_cards(page)

        # Diagnostic surfaced at INFO level so it appears in the operator's
        # log view. This tells us exactly what the packages page contains
        # when card detection fails.
        if not all_cards:
            try:
                dbg = await page.evaluate(
                    r"""() => {
                        const norm = (s) => (s||'').replace(/\s+/g,' ').trim();
                        const buttons = [...document.querySelectorAll('button')];
                        const pilih = buttons.filter(b => /\bPilih\b|\bSelect\b/i.test(norm(b.innerText)));
                        // Sample button texts to learn the real label.
                        const sampleBtns = buttons
                            .map(b => norm(b.innerText))
                            .filter(Boolean)
                            .slice(0, 12);
                        const frames = [...document.querySelectorAll('iframe')].map(f => f.src || '(no src)');
                        return {
                            url: location.href,
                            buttons: buttons.length,
                            pilih: pilih.length,
                            sampleBtns: sampleBtns,
                            iframes: frames.length,
                            iframeSrc: frames.slice(0, 3),
                        };
                    }"""
                )
                await context.emit_event(
                    f"PKG-DIAG url={dbg.get('url','')} buttons={dbg.get('buttons')} "
                    f"pilih={dbg.get('pilih')} iframes={dbg.get('iframes')} "
                    f"sampleBtns={dbg.get('sampleBtns')} iframeSrc={dbg.get('iframeSrc')}",
                    level=WorkflowEventLevel.WARN,
                )
            except Exception:  # noqa: BLE001
                pass

            # Fallback: tiket.com sometimes renders package selection inside
            # an iframe. Scan every child frame for package cards too.
            all_cards = await self._enumerate_cards_in_frames(page, context)

        if all_cards:
            chosen = await self._click_best_available_card(
                page, context, all_cards, packages, attempted, ""
            )
            if chosen is not None:
                return chosen

        await context.emit_event(
            f"package fallback exhausted (requested={packages}, "
            f"found={[c['title'] for c in all_cards]})",
            context={
                "attempted": attempted,
                "available_titles": [c["title"] for c in all_cards],
                "packages_requested": packages,
            },
            level=WorkflowEventLevel.WARN,
        )
        return None

    async def _enumerate_cards_in_frames(
        self, page: Page, context: PluginContext
    ) -> list[dict[str, Any]]:
        """Scan child frames for package cards (tiket.com sometimes uses an
        iframe for the package picker). Returns cards from the first frame
        that yields any; the frame reference is stored so later clicks scope
        correctly."""

        for frame in page.frames:
            if frame is page.main_frame:
                continue
            try:
                count = await frame.evaluate(
                    r"""() => [...document.querySelectorAll('button')].filter(
                        b => /\bPilih\b|\bSelect\b/i.test((b.innerText||'').replace(/\s+/g,' ').trim())
                    ).length"""
                )
            except Exception:  # noqa: BLE001
                continue
            if count and count > 0:
                await context.emit_event(
                    f"found {count} package buttons inside iframe {frame.url}",
                    level=WorkflowEventLevel.WARN,
                )
                self._package_frame = frame
                # Re-use the same enumeration JS but against the frame.
                return await self._enumerate_package_cards_in(frame)
        return []

    async def _poll_for_pilih(
        self, page: Page, context: PluginContext, *, timeout_ms: int = 10_000
    ) -> bool:
        """Poll until at least one Pilih/Select button is visible.

        Returns True when buttons are found, False on timeout.
        Unlike ``_wait_for_packages`` this is a lightweight probe meant
        to be called AFTER a category click when the DOM is already partially
        rendered and we just need to wait for React to flush the new cards.
        """

        budget = max(500, timeout_ms)
        elapsed = 0
        step = 300
        while elapsed < budget:
            try:
                found = await page.evaluate(
                    r"""() => {
                        for (const b of document.querySelectorAll('button')) {
                            const t = (b.innerText || '').replace(/\s+/g, ' ').trim();
                            if (/\bPilih\b|\bSelect\b/i.test(t)) return true;
                        }
                        return false;
                    }"""
                )
                if found:
                    return True
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(step / 1000)
            elapsed += step
        return False

    async def _click_best_available_card(
        self,
        page: Page,
        context: PluginContext,
        cards: list[dict[str, Any]],
        packages: list[str],
        attempted: list[str],
        category: str,
    ) -> dict[str, str] | None:
        """Click the highest-scoring available card, falling through on miss.

        We score every (input package, card) pair, sort descending, and try
        them in order. A card is dropped from the candidate set if it's
        explicitly sold out or its click fails - so a runner-up genuinely
        gets a shot, instead of us bailing on the first match.
        """

        # Build the scored matrix: list of (score, card, matched_input).
        matrix: list[tuple[float, dict[str, Any], str]] = []
        for candidate in packages:
            scored = self._score_cards(cards, candidate)
            for score, card in scored:
                if score < 50:
                    continue
                matrix.append((score, card, candidate))
        matrix.sort(key=lambda item: item[0], reverse=True)

        used_card_indexes: set[int] = set()
        for score, card, matched_input in matrix:
            if card["index"] in used_card_indexes:
                continue
            used_card_indexes.add(card["index"])
            tag = f"{category}|{matched_input}|{card.get('title', '')}" if category else f"{matched_input}|{card.get('title', '')}"
            attempted.append(tag)
            row = self._locator_for_card(page, card["index"])
            if await self._is_package_sold_out(row):
                await context.emit_event(
                    "package sold out - falling back",
                    context={
                        "matched_input": matched_input,
                        "card": card.get("title", ""),
                        "category": category or "<any>",
                        "score": round(score, 1),
                    },
                    level=WorkflowEventLevel.WARN,
                )
                continue
            if not await self._click_package_row(row, context, matched_input):
                continue
            await context.emit_event(
                "package selected",
                context={
                    "matched_input": matched_input,
                    "card": card.get("title", ""),
                    "category": category or "<any>",
                    "score": round(score, 1),
                },
            )
            return {
                "package": matched_input,
                "card_title": card.get("title", ""),
                "category": category,
                "_row_marker": "data-mans-active-row",
            }
        return None

    async def _read_category_availability(self, page: Page) -> dict[str, int]:
        """Read the right-hand sidebar that lists each category and its count."""

        try:
            payload = await page.evaluate(
                """() => {
                    const out = {};
                    const containers = document.querySelectorAll(
                        'aside, [class*=\"category\" i], [class*=\"Category\"], [class*=\"sidebar\" i], section'
                    );
                    for (const c of containers) {
                        const items = c.querySelectorAll('li, div, a, span');
                        for (const item of items) {
                            const t = (item.innerText || '').trim();
                            if (!t || t.length > 60) continue;
                            const m = t.match(/^([A-Z][A-Z0-9 ]{1,30})\\s+(\\d{1,4})$/);
                            if (m) {
                                out[m[1].trim().toLowerCase()] = parseInt(m[2], 10);
                            }
                        }
                    }
                    return out;
                }"""
            )
        except Exception:  # noqa: BLE001
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(k).lower(): int(v) for k, v in payload.items() if isinstance(v, (int, float))}

    @staticmethod
    def _expand_categories(
        requested: list[str],
        availability: dict[str, int],
    ) -> list[str]:
        """Expand prefix matches against the live sidebar.

        Example: requested ``["CAT", "FESTIVAL"]`` against availability
        ``{"festival": 2, "cat 1": 2, "cat 2": 0, "cat 3": 0}`` becomes
        ``["CAT 1", "CAT 2", "CAT 3", "FESTIVAL"]`` (ordering preserved).
        """

        if not requested:
            return list(availability.keys()) if availability else []
        if not availability:
            return list(requested)
        seen: set[str] = set()
        expanded: list[str] = []
        for entry in requested:
            target = entry.strip().lower()
            if not target:
                continue
            if target in availability:
                if entry not in seen:
                    expanded.append(entry)
                    seen.add(entry)
                continue
            # Treat as a prefix and pull every sidebar key that starts with it.
            matches = [
                key for key in availability
                if key == target or key.startswith(target + " ")
            ]
            if matches:
                # Preserve the sidebar's natural order (insertion order).
                ordered = [
                    key for key in availability.keys() if key in matches
                ]
                for match in ordered:
                    pretty = match.upper()
                    if pretty not in seen:
                        expanded.append(pretty)
                        seen.add(pretty)
            else:
                if entry not in seen:
                    expanded.append(entry)
                    seen.add(entry)
        return expanded

    @staticmethod
    def _lookup_availability(availability: dict[str, int], category: str) -> int | None:
        if not availability:
            return None
        target = category.strip().lower()
        if target in availability:
            return availability[target]
        for key, value in availability.items():
            if target in key or key in target:
                return value
        return None

    async def _enumerate_package_cards(self, page: "Page") -> "list[dict[str, Any]]":
        """Tag and return every leaf package card visible on the page."""

        return await self._run_card_enumeration(page)

    async def _enumerate_package_cards_in(self, frame: Any) -> "list[dict[str, Any]]":
        """Same as :meth:`_enumerate_package_cards` but against a child frame."""

        return await self._run_card_enumeration(frame)

    async def _run_card_enumeration(self, target: Any) -> "list[dict[str, Any]]":
        try:
            payload = await target.evaluate(_CARD_ENUMERATION_JS)
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict) and "index" in item]

    @staticmethod
    def _score_cards(
        cards: list[dict[str, Any]],
        package_name: str,
    ) -> list[tuple[float, dict[str, Any]]]:
        """Score every card against ``package_name`` and return them sorted.

        The score is the best of two comparisons:

          - against the card's extracted *title* (weighted at full strength)
          - against the card's full visible *text* (weighted slightly lower)

        Scoring against the full text makes matching robust for ANY event,
        regardless of how the package is named or where the name sits in the
        card DOM. The title comparison stays primary so a clean title match
        always wins over an incidental text hit.
        """

        target = TiketComPlugin._normalize_package_text(package_name)
        if not target:
            return []
        target_tokens = set(target.split())
        scored: list[tuple[float, dict[str, Any]]] = []
        for card in cards:
            title = TiketComPlugin._normalize_package_text(str(card.get("title", "")))
            text = TiketComPlugin._normalize_package_text(str(card.get("text", "")))

            title_score = (
                TiketComPlugin._score_pair(target, target_tokens, title)
                if title
                else 0.0
            )
            # Text match uses the same kernel but against the whole card.
            # Slightly discounted so a precise title match is preferred.
            text_score = (
                TiketComPlugin._score_pair(target, target_tokens, text) * 0.92
                if text
                else 0.0
            )
            score = max(title_score, text_score)
            if score <= 0:
                continue
            scored.append((score, card))
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored

    @staticmethod
    def _score_pair(target: str, target_tokens: set[str], candidate: str) -> float:
        """Compute a 0-100 fuzzy score between a normalised target and a
        candidate string (either a card title or full card text)."""

        if not target or not candidate:
            return 0.0
        candidate_tokens = set(candidate.split())
        # rapidfuzz primitives give us 0-100 already.
        token_set = fuzz.token_set_ratio(target, candidate)
        partial = fuzz.partial_ratio(target, candidate)
        wratio = fuzz.WRatio(target, candidate)
        # Token overlap: fraction of input words that appear as standalone
        # tokens in the candidate. Heavily penalises false matches like
        # "CAT 1 RIGHT" -> "FESTIVAL B" (no shared tokens).
        if target_tokens:
            shared = target_tokens & candidate_tokens
            overlap_ratio = len(shared) / len(target_tokens)
        else:
            overlap_ratio = 0.0

        score = max(token_set, partial, wratio)
        # Pull score down hard when no input token appears in the candidate.
        if overlap_ratio == 0 and target_tokens:
            score *= 0.4
        else:
            score = score * 0.6 + (overlap_ratio * 100) * 0.4
        # Exact substring lifts the score back to near-perfect. This is the
        # key generic signal: whatever the package is named, if the user's
        # input appears verbatim inside the card, it's a strong match.
        if target in candidate:
            score = max(score, 96.0)
        elif candidate in target:
            score = max(score, 90.0)
        # Penalise when the candidate is dramatically longer/shorter than the
        # input ONLY for title-style comparisons. For full card text the
        # length ratio is naturally tiny, so we skip the penalty when every
        # input token is present (a confident overlap match).
        len_ratio = min(len(target), len(candidate)) / max(len(target), len(candidate))
        if len_ratio < 0.3 and overlap_ratio < 1.0:
            score *= 0.85
        return float(score)

    @staticmethod
    def _normalize_package_text(value: str) -> str:
        """Lower-case + collapse whitespace + strip noise tokens for matching."""

        if not value:
            return ""
        text = value.lower()
        # Drop leading bullets / arrows / pipes that sometimes prefix titles.
        text = re.sub(r"^[\s>\u2022\-|]+", "", text)
        # Trim suffixes like ' (general sale)' or ' - sold out'.
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _locator_for_card(self, page: Page, index: int) -> Locator:
        target = self._package_frame if self._package_frame is not None else page
        return target.locator(f"[data-mans-package-card='{index}']").first

    async def _select_category(
        self, page: Page, context: PluginContext, category: str
    ) -> bool:
        """Click the sidebar/nav entry for ``category``.

        This is best-effort: if no sidebar is found the function returns
        False and the caller falls through to a full-page card scan, which
        works on layouts that render all categories together.
        """

        target = category.strip().lower()
        if not target:
            return False
        try:
            tagged = await page.evaluate(
                """(target) => {
                    // tiket.com renders the right-hand category panel as a
                    // vertical list of anchors, divs, or list items. The
                    // category name and the count may be in separate child
                    // nodes. We strip digits and whitespace before comparing
                    // so 'cat 1\\n2' -> 'cat 1'.
                    const normalize = (s) => s.toLowerCase().replace(/\\s*\\d+\\s*$/, '').trim();

                    // Priority: sidebar / aside containers.
                    const sidebarSelectors = [
                        'aside',
                        '[class*="sidebar" i]',
                        '[class*="Sidebar"]',
                        '[class*="category" i]',
                        '[class*="Category"]',
                        '[data-testid*="category" i]',
                        '[data-testid*="package" i]',
                    ];
                    for (const sel of sidebarSelectors) {
                        const containers = document.querySelectorAll(sel);
                        for (const c of containers) {
                            const items = c.querySelectorAll('a, li, button, div[role="button"], [tabindex]');
                            for (const item of items) {
                                const raw = (item.innerText || '').trim();
                                if (!raw || raw.length > 80) continue;
                                if (normalize(raw) === target) {
                                    item.setAttribute('data-mans-target-category', '1');
                                    return 'sidebar';
                                }
                            }
                        }
                    }

                    // Fallback: any heading / large-text element that equals
                    // the category name. Covers pages where the category is a
                    // collapsible <h2> header inside the package list.
                    const headings = document.querySelectorAll('h2, h3, h4, [class*="heading" i]');
                    for (const h of headings) {
                        const raw = (h.innerText || '').trim();
                        if (!raw || raw.length > 80) continue;
                        if (normalize(raw) === target) {
                            h.setAttribute('data-mans-target-category', '1');
                            return 'heading';
                        }
                    }
                    return '';
                }""",
                target,
            )
        except Exception:  # noqa: BLE001
            return False
        if not tagged:
            return False
        try:
            locator = page.locator("[data-mans-target-category='1']").first
            await locator.scroll_into_view_if_needed(timeout=1_500)
            await locator.click(timeout=2_500)
            # Clear the attribute so it doesn't bleed into the next call.
            try:
                await locator.evaluate("(el) => el.removeAttribute('data-mans-target-category')")
            except Exception:  # noqa: BLE001
                pass
            await context.emit_event(
                "selected category",
                context={"category": category, "via": str(tagged)},
            )
            return True
        except Exception as exc:  # noqa: BLE001
            await context.emit_event(
                "category click failed - continuing without category filter",
                context={"category": category, "error": str(exc).splitlines()[0]},
                level=WorkflowEventLevel.WARN,
            )
            return False

    async def _click_package_row(
        self, row: Locator, context: PluginContext, package_name: str
    ) -> bool:
        try:
            await row.scroll_into_view_if_needed(timeout=2_000)
            try:
                await row.evaluate(
                    "(el) => { el.setAttribute('data-mans-active-row', '1'); }"
                )
            except Exception:  # noqa: BLE001
                pass
            # Use JS to locate and click the Pilih/Select button so that
            # SVG icon children and whitespace don't cause :has-text to miss.
            clicked = await row.evaluate(
                """(root) => {
                    const buttons = root.querySelectorAll('button');
                    for (const b of buttons) {
                        const t = (b.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (/\\bPilih\\b|\\bSelect\\b/i.test(t)) {
                            b.click();
                            return true;
                        }
                    }
                    return false;
                }"""
            )
            if not clicked:
                return False
            await context.sync.wait_for_dom_settle(
                page=row.page, quiet_ms=200, timeout_ms=2_000
            )
            return True
        except Exception as exc:  # noqa: BLE001
            await context.emit_event(
                "click on package row failed",
                level=WorkflowEventLevel.WARN,
                context={"package": package_name, "error": str(exc).splitlines()[0]},
            )
            return False

    async def _is_package_sold_out(self, row: Locator) -> bool:
        """Return True only when the row carries an *explicit* sold-out signal.

        We must avoid false positives caused by lazy rendering: a row below
        the fold can have an off-screen 'Pilih' button that ``is_visible``
        rejects. Scrolling the row into view eliminates that ambiguity.
        """

        try:
            await row.scroll_into_view_if_needed(timeout=2_000)
        except Exception:  # noqa: BLE001
            pass
        try:
            text = (await row.inner_text()).lower()
        except Exception:  # noqa: BLE001
            return False
        explicit_markers = (
            "sold out",
            "habis terjual",
            "tiket habis",
            "stok habis",
            "tidak tersedia",
            "unavailable",
            "out of stock",
        )
        if any(token in text for token in explicit_markers):
            return True
        # Treat the row as available unless its action button is literally
        # disabled. Missing/invisible buttons are not enough evidence: lazy
        # render or animations may delay them, and we'd rather attempt the
        # click and fail than skip an available package.
        try:
            # Find the Pilih/Select action button inside this card using JS
            # so that SVG children / whitespace don't interfere.
            disabled_result = await row.evaluate(
                """(root) => {
                    const buttons = root.querySelectorAll('button');
                    for (const b of buttons) {
                        const t = (b.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (/\\bPilih\\b|\\bSelect\\b/i.test(t)) {
                            if (b.disabled || b.getAttribute('aria-disabled') === 'true') {
                                return 'disabled';
                            }
                            return 'enabled';
                        }
                    }
                    return 'not_found';
                }"""
            )
            if disabled_result == "disabled":
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    @staticmethod
    def _best_match(events: list[dict[str, str]], target: str) -> dict[str, str] | None:
        if not events:
            return None
        target_norm = target.lower()
        best: tuple[int, dict[str, str]] | None = None
        for entry in events:
            title = entry.get("title", "").lower()
            if not title:
                continue
            score = fuzz.partial_ratio(target_norm, title)
            if best is None or score > best[0]:
                best = (score, entry)
        if best is None:
            return None
        if best[0] < 55:
            # Too weak a match - fall back to the first card.
            return events[0]
        return best[1]

    async def _wait_for_packages(self, page: Page, context: PluginContext) -> None:
        """Wait for the packages page to fully render all Pilih/Select buttons."""

        speed = max(0.25, float(getattr(context.workflow_settings, "sync_speed_multiplier", 1.0)))

        # If the browser is still mid-navigation, wait for domcontentloaded first.
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=int(8_000 * speed))
        except Exception:  # noqa: BLE001
            pass

        # Clear skeletons / spinners.
        try:
            await context.sync.wait_for_disappear(
                page,
                (
                    "[data-testid*='skeleton' i]",
                    "[class*='skeleton' i]",
                    "[role='progressbar']",
                    "[aria-busy='true']",
                ),
                timeout_ms=int(6_000 * speed),
            )
        except Exception:  # noqa: BLE001
            pass

        # tiket.com's Pilih button may contain an SVG chevron as a child so
        # its innerText can be "Pilih" or "▼ Pilih" or have whitespace
        # around it.  We probe with a JS-based check that is whitespace and
        # icon-agnostic.
        async def _has_pilih() -> bool:
            try:
                found = await page.evaluate(
                    """() => {
                        const all = document.querySelectorAll('button');
                        for (const b of all) {
                            const t = (b.innerText || '').replace(/\\s+/g, ' ').trim();
                            if (/\\bPilih\\b|\\bSelect\\b/i.test(t)) return true;
                        }
                        return false;
                    }"""
                )
                return bool(found)
            except Exception:  # noqa: BLE001
                return False

        # Poll until Pilih buttons appear, up to the speed-scaled budget.
        budget = int(15_000 * speed)
        elapsed = 0
        step = 500
        while elapsed < budget:
            if await _has_pilih():
                break
            await asyncio.sleep(step / 1000)
            elapsed += step

        if not await _has_pilih():
            await context.emit_event(
                "no 'Pilih/Select' buttons found in time - the page may have a queue or CAPTCHA",
                level=WorkflowEventLevel.WARN,
            )

        # Lazy-scroll to surface any below-the-fold packages.
        previous = -1
        for _ in range(6):
            try:
                count = await page.evaluate(
                    r"""() => [...document.querySelectorAll('button')].filter(
                        b => /\bPilih\b|\bSelect\b/i.test((b.innerText || '').replace(/\s+/g,' ').trim())
                    ).length"""
                )
            except Exception:  # noqa: BLE001
                break
            if count == previous:
                break
            previous = count
            try:
                await page.evaluate("() => window.scrollBy(0, window.innerHeight * 0.7)")
            except Exception:  # noqa: BLE001
                break
            await asyncio.sleep(0.3)
        # Scroll back to top so the sidebar category links are visible.
        try:
            await page.evaluate("() => window.scrollTo({ top: 0, behavior: 'instant' })")
        except Exception:  # noqa: BLE001
            pass
        # Brief settle after returning to top.
        await context.sync.wait_for_dom_settle(
            page, quiet_ms=int(200 * speed), timeout_ms=int(1_500 * speed)
        )

    async def _set_quantity(self, page: Page, context: PluginContext, quantity: int) -> None:
        if quantity <= 1:
            return

        # Wait for the stepper row to render. tiket.com's expanded package
        # card carries the heading 'Jumlah Tiket' followed by a 'Pax' row
        # containing two icon-only buttons and the count between them.
        try:
            await page.wait_for_selector("text=Jumlah Tiket", timeout=8_000)
        except Exception:  # noqa: BLE001
            pass
        await context.sync.wait_for_dom_settle(page, quiet_ms=250, timeout_ms=2_500)

        speed = max(0.25, float(getattr(context.workflow_settings, "sync_speed_multiplier", 1.0)))

        # Locate the expanded card. Prefer the card we marked when clicking
        # 'Pilih'; fall back to any visible 'Pesan' button's container.
        card = await self._resolve_expanded_card(page)
        if card is None:
            await context.emit_event(
                "could not locate expanded package card - skipping quantity bump",
                level=WorkflowEventLevel.WARN,
            )
            return

        await card.scroll_into_view_if_needed(timeout=2_000)

        # Find the row that contains the count + stepper buttons. We look
        # for an element whose text starts with 'Pax' and contains a digit
        # neighbour. Within that row, the second button is '(+)'.
        plus_button = await self._resolve_plus_button(card)
        if plus_button is None:
            await context.emit_event(
                "stepper '+' button not found inside the package card",
                level=WorkflowEventLevel.WARN,
            )
            return

        max_attempts = max(quantity * 4, 20)
        attempts = 0
        last_count: int | None = await self._read_card_quantity(card)
        if last_count is None:
            last_count = 1
        stagnant = 0
        while attempts < max_attempts:
            current = await self._read_card_quantity(card)
            if current is not None and current >= quantity:
                last_count = current
                break
            try:
                if not await plus_button.is_enabled(timeout=1_000):
                    await context.emit_event(
                        "stepper '+' button disabled - reached maximum",
                        context={"current": current, "target": quantity},
                        level=WorkflowEventLevel.WARN,
                    )
                    break
                await plus_button.click(timeout=int(2_500 * speed))
            except Exception as exc:  # noqa: BLE001
                await context.emit_event(
                    "stepper '+' click failed",
                    level=WorkflowEventLevel.WARN,
                    context={"detail": str(exc).splitlines()[0]},
                )
                break
            await context.sync.wait_for_dom_settle(
                page, quiet_ms=int(120 * speed), timeout_ms=int(800 * speed)
            )
            new_count = await self._read_card_quantity(card)
            if new_count is not None and last_count is not None and new_count == last_count:
                stagnant += 1
                if stagnant >= 3:
                    await context.emit_event(
                        "ticket quantity is no longer increasing - reached maximum",
                        level=WorkflowEventLevel.WARN,
                        context={"current": new_count, "target": quantity},
                    )
                    break
            else:
                stagnant = 0
            last_count = new_count if new_count is not None else last_count
            attempts += 1

        await context.emit_event(
            "set ticket quantity",
            context={"requested": quantity, "applied": last_count, "iterations": attempts},
        )

    async def _resolve_expanded_card(self, page: Page) -> Locator | None:
        """Return the package card that contains the live stepper + Pesan button."""

        # Preferred: the card we marked when clicking Pilih.
        marked = page.locator("[data-mans-active-row='1']").first
        try:
            if await marked.is_visible(timeout=600):
                return marked
        except Exception:  # noqa: BLE001
            pass

        # Fallback: the only visible card that contains both 'Pax' text and
        # a 'Pesan' / 'Book' button. tiket.com renders only one expanded
        # package at a time, so this is unambiguous.
        candidates = page.locator(
            ":has-text('Pax'):has(button:has-text('Pesan')), "
            ":has-text('Pax'):has(button:has-text('Book'))"
        )
        try:
            count = await candidates.count()
        except Exception:  # noqa: BLE001
            return None
        # Pick the deepest visible match (largest depth = most specific card).
        for index in range(min(count, 20)):
            entry = candidates.nth(index)
            try:
                if await entry.is_visible(timeout=400):
                    return entry
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _resolve_plus_button(self, card: Locator) -> Locator | None:
        """Locate the (+) button inside an expanded package card.

        The button has no accessible label and its child SVG carries no
        deterministic class. We rely on positional logic: the stepper row
        renders exactly two icon buttons surrounding a numeric count, so the
        second of those two buttons is always '+'.
        """

        # Strategy 1: aria-label heuristics (works when tiket.com adds them).
        labelled_selectors = (
            "button[aria-label*='plus' i]",
            "button[aria-label*='increase' i]",
            "button[aria-label*='add' i]",
            "button[aria-label*='tambah' i]",
            "button[data-testid*='plus' i]",
            "button[data-testid*='add' i]",
            "button[data-testid*='increase' i]",
        )
        for selector in labelled_selectors:
            try:
                locator = card.locator(selector).first
                if await locator.is_visible(timeout=400):
                    return locator
            except Exception:  # noqa: BLE001
                continue

        # Strategy 2: positional. Use JS to find the deepest element whose
        # text starts with 'Pax' and contains exactly two button children
        # surrounding a numeric label. Return a unique data-attribute we set
        # on the '+' button so we can locate it from Playwright afterwards.
        try:
            tagged = await card.evaluate(
                """(root) => {
                    const target = (() => {
                        const all = root.querySelectorAll('*');
                        let best = null;
                        for (const node of all) {
                            const text = (node.innerText || '').trim();
                            if (!/^Pax\\b/i.test(text)) continue;
                            const buttons = node.querySelectorAll(':scope button');
                            if (buttons.length < 2) continue;
                            // Prefer the deepest match that has at most two
                            // icon-only buttons (others are siblings of the
                            // stepper, e.g. 'Detail').
                            const iconOnly = Array.from(buttons).filter((b) => {
                                const t = (b.innerText || '').trim();
                                return !t || /^[+\\-\\u2212\\u00d7]$/.test(t) || t.length <= 2;
                            });
                            if (iconOnly.length >= 2) best = iconOnly;
                        }
                        return best;
                    })();
                    if (!target) return false;
                    const plus = target[target.length - 1];
                    plus.setAttribute('data-mans-stepper-plus', '1');
                    return true;
                }"""
            )
        except Exception:  # noqa: BLE001
            return None
        if not tagged:
            return None
        return card.locator("[data-mans-stepper-plus='1']").first

    async def _click_pesan_button(self, page: Page, context: PluginContext) -> None:
        """Click the 'Pesan' button inside the currently expanded card."""

        card = await self._resolve_expanded_card(page)
        if card is not None:
            try:
                clicked = await card.evaluate(
                    """(root) => {
                        const labels = ['Pesan', 'Book', 'Checkout', 'Order now'];
                        const buttons = root.querySelectorAll('button');
                        for (const b of buttons) {
                            const t = (b.innerText || '').replace(/\\s+/g, ' ').trim();
                            if (labels.some(l => new RegExp('\\\\b' + l + '\\\\b', 'i').test(t))) {
                                if (!b.disabled) { b.click(); return t; }
                            }
                        }
                        return '';
                    }"""
                )
                if clicked:
                    await context.emit_event(
                        "clicked Pesan inside active card",
                        context={"label": str(clicked)},
                        level=WorkflowEventLevel.DEBUG,
                    )
                    return
            except Exception:  # noqa: BLE001
                pass
        # Fallback: page-wide search.
        await self._click_button_by_text(
            page, ("pesan", "book", "checkout"), context, "pesan/checkout"
        )

    async def _read_card_quantity(self, card: Locator) -> int | None:
        """Read the quantity number displayed inside the active package card."""

        try:
            value = await card.evaluate(
                """(el) => {
                    // Find the stepper row, then the numeric label sitting
                    // between the two icon buttons.
                    const rows = el.querySelectorAll('*');
                    for (const node of rows) {
                        const text = (node.innerText || '').trim();
                        if (!text) continue;
                        if (!/^Pax\\b/i.test(text)) continue;
                        const candidates = node.querySelectorAll('span, div, p, input');
                        for (const c of candidates) {
                            const t = (c.value || c.innerText || '').trim();
                            if (/^\\d{1,3}$/.test(t)) {
                                return parseInt(t, 10);
                            }
                        }
                    }
                    // Fallback: any 1-3 digit number that sits next to '/pax'.
                    const all = el.querySelectorAll('*');
                    for (const node of all) {
                        const t = (node.innerText || '').trim();
                        if (/^\\d{1,3}$/.test(t)) {
                            return parseInt(t, 10);
                        }
                    }
                    return null;
                }"""
            )
        except Exception:  # noqa: BLE001
            return None
        if isinstance(value, (int, float)):
            return int(value)
        return None

    # -------------------------------------------------------- order/visitors

    async def _fill_order_details(self, page: Page, context: PluginContext) -> None:
        await context.emit_event("filling buyer details (Detail Pemesanan)")
        await self._select_salutation(page, context, context.profile.gender)

        snapshot = await context.dom_extractor.extract(page)
        report = context.form_detector.analyze(snapshot)
        result = await context.autofill.fill_report(page, report, context.profile)
        await context.emit_event("buyer details autofill", context=result.summary())

        # Set the country / "Negara tempat tinggal" select if available.
        country = (context.profile.address.country or "").strip()
        if country:
            await self._select_country(page, context, country)

    async def _fill_visitor_details(self, page: Page, context: PluginContext, quantity: int) -> None:
        if quantity <= 0:
            return
        attendees = list(context.profile.attendees)
        if not attendees:
            await context.emit_event(
                "no attendees configured on profile - skipping visitor details",
                level=WorkflowEventLevel.WARN,
            )
            return
        await context.emit_event("filling visitor details (Detail Pengunjung)")
        snapshot = await context.dom_extractor.extract(page)
        report = context.form_detector.analyze(snapshot)
        result = await context.autofill.fill_report(
            page, report, context.profile, attendees=attendees
        )
        await context.emit_event("visitor details autofill", context=result.summary())

    async def _select_salutation(self, page: Page, context: PluginContext, gender: str | None) -> None:
        if not gender:
            return
        token = gender.strip().lower()
        if token in {"m", "male", "mr", "tuan", "pria", "laki-laki"}:
            label = "tuan"
        elif token in {"f", "female", "mrs", "ms", "nyonya", "nona", "wanita", "perempuan"}:
            label = "nyonya"
        else:
            return
        try:
            radios = page.locator("input[type='radio']")
            count = await radios.count()
        except Exception:  # noqa: BLE001
            return
        for index in range(count):
            radio = radios.nth(index)
            try:
                value = (await radio.get_attribute("value") or "").lower()
                radio_id = (await radio.get_attribute("id") or "")
            except Exception:  # noqa: BLE001
                continue
            label_text = ""
            if radio_id:
                try:
                    label_text = (
                        await page.locator(f"label[for='{radio_id}']").first.inner_text()
                    ).lower()
                except Exception:  # noqa: BLE001
                    label_text = ""
            if label in {value, label_text} or label in label_text:
                try:
                    await radio.check()
                    await context.emit_event(
                        "selected salutation", context={"label": label}
                    )
                    return
                except Exception:  # noqa: BLE001
                    continue

    async def _select_country(self, page: Page, context: PluginContext, country: str) -> None:
        target = country.strip().lower()
        # The country control is a custom dropdown - click the trigger first.
        triggers = (
            "div:has-text('Negara tempat tinggal')",
            "[data-testid='nationality']",
            "[data-testid*='country' i]",
            "div[role='button']:has-text('Negara')",
        )
        for selector in triggers:
            try:
                trigger = page.locator(selector).first
                if not await trigger.is_visible(timeout=1_000):
                    continue
                await trigger.click()
                await asyncio.sleep(0.3)
                break
            except Exception:  # noqa: BLE001
                continue
        # Then type the country name into the search box that appears.
        try:
            search = page.locator(
                "input[placeholder*='Cari' i], input[placeholder*='Search' i]"
            ).first
            if await search.is_visible(timeout=1_500):
                await search.fill("")
                await search.type(country, delay=15)
                await asyncio.sleep(0.4)
        except Exception:  # noqa: BLE001
            pass
        # Finally pick the matching option.
        try:
            options = page.locator("li, [role='option']")
            total = await options.count()
        except Exception:  # noqa: BLE001
            total = 0
        for index in range(min(total, 100)):
            option = options.nth(index)
            try:
                if not await option.is_visible():
                    continue
                text = (await option.inner_text()).strip().lower()
            except Exception:  # noqa: BLE001
                continue
            if target == text or target in text:
                try:
                    await option.click()
                    await context.emit_event("selected country", context={"country": country})
                    return
                except Exception:  # noqa: BLE001
                    continue

    # ------------------------------------------------------------- utilities

    async def _click_buy_ticket_cta(self, page: Page, context: PluginContext) -> bool:
        """Click the main 'Beli tiket sekarang' / 'Buy ticket now' CTA.

        Tiket.com event detail pages render a sticky CTA panel on the right
        of the hero (see screenshot). The button has no stable test id, so
        we match on its visible text with several phrasings, scroll the
        button into view (it is often below the fold on small viewports)
        and re-resolve on every attempt to survive React rerenders.
        """

        speed = max(0.25, float(getattr(context.workflow_settings, "sync_speed_multiplier", 1.0)))
        # Make sure hydration is complete and any 'Tiket tersedia, beli sebelum
        # kebahisan' panel has rendered.
        await context.sync.wait_for_dom_settle(
            page, quiet_ms=int(350 * speed), timeout_ms=int(4_000 * speed)
        )

        labels = (
            "Beli tiket sekarang",
            "Beli Tiket Sekarang",
            "Beli tiket",
            "Pesan tiket",
            "Pesan tiket sekarang",
            "Buy ticket now",
            "Buy ticket",
            "Buy now",
            "Get tickets",
            "Lihat tiket",
            "Select ticket",
        )

        max_attempts = 4
        for attempt in range(max_attempts):
            for label in labels:
                # Use Playwright's role-based text matcher; falls back to a
                # generic ``button:has-text`` if no role is found.
                target = page.get_by_role("button", name=label, exact=False)
                try:
                    if await target.first.is_visible(timeout=int(800 * speed)):
                        await target.first.scroll_into_view_if_needed(timeout=2_000)
                        await target.first.click(timeout=int(3_500 * speed))
                        await context.emit_event(
                            "clicked buy/select tickets",
                            context={"label": label, "attempt": attempt + 1},
                            level=WorkflowEventLevel.DEBUG,
                        )
                        return True
                except Exception:  # noqa: BLE001
                    pass

                generic = page.locator(f"button:has-text('{label}')").first
                try:
                    if await generic.is_visible(timeout=int(600 * speed)):
                        await generic.scroll_into_view_if_needed(timeout=2_000)
                        await generic.click(timeout=int(3_500 * speed))
                        await context.emit_event(
                            "clicked buy/select tickets",
                            context={"label": label, "attempt": attempt + 1, "selector": "has-text"},
                            level=WorkflowEventLevel.DEBUG,
                        )
                        return True
                except Exception:  # noqa: BLE001
                    continue

            # Try anchor-style "Beli tiket sekarang" links + role=link
            try:
                link = page.get_by_role("link", name="Beli tiket sekarang", exact=False).first
                if await link.is_visible(timeout=int(600 * speed)):
                    await link.scroll_into_view_if_needed(timeout=2_000)
                    await link.click(timeout=int(3_500 * speed))
                    await context.emit_event(
                        "clicked buy/select tickets (anchor)",
                        context={"attempt": attempt + 1},
                        level=WorkflowEventLevel.DEBUG,
                    )
                    return True
            except Exception:  # noqa: BLE001
                pass

            # Encourage tiket.com to load any below-the-fold CTA, then settle.
            try:
                await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight * 0.4)")
            except Exception:  # noqa: BLE001
                pass
            await context.sync.wait_for_dom_settle(
                page, quiet_ms=int(250 * speed), timeout_ms=int(2_000 * speed)
            )
            try:
                await page.evaluate("() => window.scrollTo(0, 0)")
            except Exception:  # noqa: BLE001
                pass

        return False

    async def _click_button_by_text(
        self,
        page: Page,
        texts: tuple[str, ...],
        context: PluginContext,
        description: str,
        *,
        required: bool = False,
    ) -> bool:
        try:
            buttons = page.locator("button, a[role='button'], [role='button'], input[type='submit']")
            count = await buttons.count()
        except Exception:  # noqa: BLE001
            count = 0
        for index in range(min(count, 200)):
            button = buttons.nth(index)
            try:
                if not (await button.is_visible() and await button.is_enabled()):
                    continue
                label = (await button.inner_text()).strip().lower()
            except Exception:  # noqa: BLE001
                continue
            if not label:
                try:
                    label = (await button.get_attribute("aria-label") or "").lower()
                except Exception:  # noqa: BLE001
                    label = ""
            if not label:
                continue
            if any(text in label for text in texts):
                try:
                    await button.scroll_into_view_if_needed(timeout=1_500)
                    await button.click()
                    await context.emit_event(
                        f"clicked {description}",
                        context={"text": label},
                        level=WorkflowEventLevel.DEBUG,
                    )
                    return True
                except Exception:  # noqa: BLE001
                    continue
        if required:
            raise HumanInterventionRequired(
                f"could not click {description} on tiket.com",
                url=page.url,
            )
        return False

    async def _await_human_when_needed(
        self,
        page: Page,
        context: PluginContext,
        *,
        extra_keywords: tuple[str, ...] = (),
    ) -> None:
        detector = HumanInterventionDetector()
        signal = await detector.detect(page)
        if signal is None and extra_keywords:
            try:
                content = await page.evaluate(
                    "() => (document.body ? document.body.innerText.slice(0, 4000).toLowerCase() : '')"
                )
            except Exception:  # noqa: BLE001
                content = ""
            if any(keyword in content for keyword in extra_keywords):
                await context.request_human(
                    "OTP / verification step detected on tiket.com. Complete it in the browser, then resume.",
                    url=page.url,
                )
                return
        if signal is None:
            return
        await context.request_human(signal.detail, url=signal.url)
        if context.is_aborted():
            raise HumanInterventionRequired(signal.reason, url=signal.url)
