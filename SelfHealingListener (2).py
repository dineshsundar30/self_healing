"""
SelfHealingListener.py
======================
Robot Framework Listener (API v3) — Self-healing engine for Appium XPath locators.

╔══════════════════════════════════════════════════════════════════════════════╗
║  TO ENABLE HEALING IN SETUP / TEARDOWN — change these two lines:           ║
║      HEAL_IN_SETUP    = False   →   HEAL_IN_SETUP    = True                ║
║      HEAL_IN_TEARDOWN = False   →   HEAL_IN_TEARDOWN = True                ║
╚══════════════════════════════════════════════════════════════════════════════╝

SUPPRESSION RULES  (healing is completely skipped when ANY rule matches)
------------------------------------------------------------------------
1. Run Keyword And Ignore Error / Continue On Failure / Return Status
2. Negative-assertion keywords  (Element Should Not Exist, etc.)
3. Check Elements Displayed  status=FALSE  (project-specific)
4. Test Setup phase   (controlled by HEAL_IN_SETUP  flag)
5. Test Teardown phase (controlled by HEAL_IN_TEARDOWN flag)
6. Locator value is empty
7. Locator is in the per-locator unhealable cache

PERFORMANCE DESIGN
------------------
• Variable lookup result is cached per locator string — only looked up once.
• page_source is fetched only when all guards pass (healing will actually run).
• No console output after the one-time "ENGINE ACTIVE" banner.
• Suppressed / rate-limited paths raise immediately — zero extra work.
• _log() and _find_variable_name() never block the test thread on failure.
"""

import time
import traceback
from collections import defaultdict

from robot.libraries.BuiltIn import BuiltIn
from HealerLogic import find_healed_locator


class SelfHealingListener:

    ROBOT_LISTENER_API_VERSION = 3

    # ╔══════════════════════════════════════════════════════════════╗
    # ║           ★  CONFIGURATION — EDIT HERE  ★                  ║
    # ╚══════════════════════════════════════════════════════════════╝

    HEAL_IN_SETUP     = False   # True  → heal during Test Setup keywords
    HEAL_IN_TEARDOWN  = False   # True  → heal during Test Teardown keywords
    UNHEALABLE_TTL    = 15.0    # seconds to skip re-healing after a proven failure
    PERSISTENT_FAIL_THRESHOLD = 3   # badge turns red after this many failures

    # ── Keyword names that suppress healing ──────────────────────────────────

    _IGNORE_ERROR_KEYWORDS = frozenset({
        "run keyword and ignore error",
        "run keyword and continue on failure",
        "run keyword and return status",
        "run keyword and expect error",
        "run keywords",
        "wait until keyword succeeds",
    })

    _NEGATIVE_KEYWORDS = frozenset({
        "element should not be visible",
        "element should not exist",
        "page should not contain element",
        "page should not contain",
        "wait until element is not visible",
        "wait until page does not contain element",
        "check elements displayed",
    })

    # ────────────────────────────────────────────────────────────────────────

    def __init__(self):
        self.webdriver_patched            = False
        self.unhealable_cache: dict       = {}                # {xpath: expiry_ts}
        self.failure_counts: dict         = defaultdict(int)  # {xpath: count}
        self._suppress_stack: list        = []
        self._in_setup                    = False
        self._in_teardown                 = False
        # Cache: {xpath_str: var_name} — avoid calling get_variables() repeatedly
        self._var_name_cache: dict        = {}

    # ────────────────────────────────────────────────────────────────────────
    # Listener hooks
    # ────────────────────────────────────────────────────────────────────────

    def start_suite(self, data, result):
        # ONE-TIME console banner — nothing else ever goes to console
        print(
            f"\n[SELF-HEAL] Engine loaded | "
            f"HEAL_IN_SETUP={self.HEAL_IN_SETUP} | "
            f"HEAL_IN_TEARDOWN={self.HEAL_IN_TEARDOWN}",
            flush=True
        )

    def start_test(self, data, result):
        self._in_setup    = False
        self._in_teardown = False
        self._suppress_stack.clear()

    def end_test(self, data, result):
        self._in_setup    = False
        self._in_teardown = False
        self._suppress_stack.clear()

    def start_keyword(self, data, result):
        kw_name    = (data.name or "").lower().strip()
        kw_type    = (getattr(data, 'type', '') or '').lower()
        args_upper = [str(a).upper() for a in (data.args or [])]

        if kw_type == 'setup':
            self._in_setup    = True
            self._in_teardown = False
        elif kw_type == 'teardown':
            self._in_teardown = True
            self._in_setup    = False

        reason = self._suppression_reason_for(kw_name, args_upper)
        if reason:
            self._suppress_stack.append(reason)

        self._patch_webdriver()

    def end_keyword(self, data, result):
        kw_name    = (data.name or "").lower().strip()
        kw_type    = (getattr(data, 'type', '') or '').lower()
        args_upper = [str(a).upper() for a in (data.args or [])]

        if self._suppression_reason_for(kw_name, args_upper) and self._suppress_stack:
            self._suppress_stack.pop()

        if kw_type == 'setup':
            self._in_setup = False
        elif kw_type == 'teardown':
            self._in_teardown = False

    # ────────────────────────────────────────────────────────────────────────
    # Suppression helpers
    # ────────────────────────────────────────────────────────────────────────

    def _suppression_reason_for(self, kw_name: str, args_upper: list):
        if kw_name in self._IGNORE_ERROR_KEYWORDS:
            return f"ignore-error wrapper '{kw_name}'"
        if kw_name in self._NEGATIVE_KEYWORDS:
            return f"negative-assertion '{kw_name}'"
        if "STATUS=FALSE" in args_upper or "FALSE" in args_upper:
            return f"status=FALSE in '{kw_name}'"
        return None

    def _get_suppression(self):
        """Return (is_suppressed: bool, reason: str). Fast path — no I/O."""
        if self._suppress_stack:
            return True, self._suppress_stack[-1]
        if self._in_setup and not self.HEAL_IN_SETUP:
            return True, "Test Setup (set HEAL_IN_SETUP=True to enable)"
        if self._in_teardown and not self.HEAL_IN_TEARDOWN:
            return True, "Test Teardown (set HEAL_IN_TEARDOWN=True to enable)"
        return False, ""

    # ────────────────────────────────────────────────────────────────────────
    # Variable name resolver  (cached — expensive only on first call per xpath)
    # ────────────────────────────────────────────────────────────────────────

    def _find_variable_name(self, xpath_value: str) -> str:
        if xpath_value in self._var_name_cache:
            return self._var_name_cache[xpath_value]
        try:
            variables = BuiltIn().get_variables()
            for var_name, var_val in variables.items():
                if isinstance(var_val, str) and var_val == xpath_value:
                    self._var_name_cache[xpath_value] = var_name
                    return var_name
        except Exception:
            pass
        self._var_name_cache[xpath_value] = "<unknown variable>"
        return "<unknown variable>"

    # ────────────────────────────────────────────────────────────────────────
    # WebDriver patch
    # ────────────────────────────────────────────────────────────────────────

    def _patch_webdriver(self):
        if self.webdriver_patched:
            return

        try:
            from appium.webdriver.webdriver import WebDriver
            from selenium.webdriver.common.by import By
        except ImportError as exc:
            self._log(f"[SELF-HEAL] Appium import failed — engine disabled: {exc}", "WARN")
            self.webdriver_patched = True
            return

        if getattr(WebDriver.find_element, '_is_healed', False):
            self.webdriver_patched = True
            return

        original_find_element = WebDriver.find_element
        unhealable_cache       = self.unhealable_cache
        failure_counts         = self.failure_counts
        listener_ref           = self

        def healed_find_element(driver_self, by='id', value=None):

            # ── STEP 1: Normal find — fast/happy path ───────────────────────
            captured_error = None
            try:
                return original_find_element(driver_self, by, value)
            except Exception as exc:
                captured_error = exc   # copy immediately — Python deletes exc

            # ── STEP 2: Normalise ───────────────────────────────────────────
            value_str = str(value).strip() if value is not None else ""
            if not value_str:
                raise captured_error   # nothing to heal

            # ── STEP 3: Suppression check (no I/O — immediate) ─────────────
            suppressed, suppress_reason = listener_ref._get_suppression()
            if suppressed:
                # Log suppression at INFO so it is visible in the RF report
                # but do NOT touch page_source or get_variables (expensive)
                failure_counts[value_str] += 1
                listener_ref._log(
                    f"<span style='color:#888'>[SELF-HEAL] SUPPRESSED &mdash; "
                    f"{suppress_reason} | xpath: <code>{value_str[:100]}</code></span>",
                    "INFO", html=True)
                raise captured_error

            # ── STEP 4: Rate-limit check (no I/O — immediate) ──────────────
            now = time.time()
            if unhealable_cache.get(value_str, 0) > now:
                failure_counts[value_str] += 1
                remaining = unhealable_cache[value_str] - now
                listener_ref._log(
                    f"<span style='color:#888'>[SELF-HEAL] RATE-LIMITED "
                    f"({remaining:.0f}s) | xpath: <code>{value_str[:100]}</code></span>",
                    "INFO", html=True)
                raise captured_error

            # ── STEP 5: Count failure + resolve variable name ───────────────
            # Variable name lookup is cached — only slow on first call per xpath
            failure_counts[value_str] += 1
            fail_count = failure_counts[value_str]
            var_name   = listener_ref._find_variable_name(value_str)

            # ── STEP 6: Log trigger ─────────────────────────────────────────
            badge_colour = "#cc0000" if fail_count >= listener_ref.PERSISTENT_FAIL_THRESHOLD else "#e67e00"
            badge_text   = f"PERSISTENT FAIL #{fail_count}" if fail_count >= listener_ref.PERSISTENT_FAIL_THRESHOLD else f"FAIL #{fail_count}"

            listener_ref._log(
                f"<div style='border:2px solid {badge_colour};border-radius:4px;"
                f"padding:6px 10px;margin:4px 0;background:#fff8f0'>"
                f"<b style='color:{badge_colour}'>&#9888; SELF-HEAL TRIGGERED</b> "
                f"<span style='background:{badge_colour};color:white;padding:1px 6px;"
                f"border-radius:3px;font-size:11px'>{badge_text}</span><br>"
                f"<b>Variable :</b> <code style='color:#9b59b6'>{var_name}</code><br>"
                f"<b>XPath    :</b> <code>{value_str}</code><br>"
                f"<b>Error    :</b> {type(captured_error).__name__}: "
                f"{str(captured_error)[:200]}"
                f"</div>",
                "WARN", html=True)

            # ── STEP 7: Fetch page source (only reaches here when healing runs)
            try:
                page_source = driver_self.page_source
            except Exception as ps_exc:
                listener_ref._log(
                    f"[SELF-HEAL] Cannot get page_source for {var_name}: {ps_exc}",
                    "WARN")
                unhealable_cache[value_str] = now + listener_ref.UNHEALABLE_TTL
                raise captured_error

            # ── STEP 8: Run healer logic ────────────────────────────────────
            try:
                new_locator = find_healed_locator(value_str, page_source)
            except Exception:
                listener_ref._log(
                    f"[SELF-HEAL] HealerLogic exception for {var_name}:<br>"
                    f"<pre>{traceback.format_exc()}</pre>",
                    "WARN", html=True)
                unhealable_cache[value_str] = now + listener_ref.UNHEALABLE_TTL
                raise captured_error

            # ── STEP 9: No match ────────────────────────────────────────────
            if not new_locator:
                listener_ref._log(
                    f"<div style='border:2px solid #cc0000;border-radius:4px;"
                    f"padding:6px 10px;margin:4px 0;background:#fff0f0'>"
                    f"<b style='color:#cc0000'>&#10007; NO MATCHING XPATH FOUND</b><br>"
                    f"<b>Variable :</b> <code style='color:#9b59b6'>{var_name}</code><br>"
                    f"<b>XPath    :</b> <code>{value_str}</code><br>"
                    f"<b>Failures :</b> {fail_count}<br>"
                    f"<i>Causes: element absent | tokens too generic | "
                    f"mid-transition | resource-id renamed with no similar ID</i>"
                    f"</div>",
                    "WARN", html=True)
                unhealable_cache[value_str] = now + listener_ref.UNHEALABLE_TTL
                raise captured_error

            # ── STEP 10: Try healed locator ─────────────────────────────────
            healed_xpath = (
                new_locator[len("xpath="):]
                if new_locator.startswith("xpath=")
                else new_locator
            )

            listener_ref._log(
                f"<div style='border:2px solid #0077cc;border-radius:4px;"
                f"padding:6px 10px;margin:4px 0;background:#f0f8ff'>"
                f"<b style='color:#0077cc'>&#128270; HEAL CANDIDATE — retrying</b><br>"
                f"<b>Variable :</b> <code style='color:#9b59b6'>{var_name}</code><br>"
                f"<b>Original :</b> <code style='color:#cc0000'>{value_str}</code><br>"
                f"<b>Healed   :</b> <code style='color:#007700'>{healed_xpath}</code>"
                f"</div>",
                "INFO", html=True)

            try:
                found = original_find_element(driver_self, by=By.XPATH, value=healed_xpath)

                listener_ref._log(
                    f"<div style='border:2px solid #007700;border-radius:4px;"
                    f"padding:6px 10px;margin:4px 0;background:#f0fff0'>"
                    f"<b style='color:#007700'>&#10003; SELF-HEAL SUCCESS</b><br>"
                    f"<b>Variable :</b> <code style='color:#9b59b6'>{var_name}</code><br>"
                    f"<b>Original :</b> <code style='color:#cc0000'>{value_str}</code><br>"
                    f"<b>Healed   :</b> <code style='color:#007700'>{healed_xpath}</code><br>"
                    f"<b>After {fail_count} failure(s)</b>"
                    f"</div>",
                    "INFO", html=True)

                failure_counts[value_str] = 0
                return found

            except Exception as retry_exc:
                listener_ref._log(
                    f"<div style='border:2px solid #cc0000;border-radius:4px;"
                    f"padding:6px 10px;margin:4px 0;background:#fff0f0'>"
                    f"<b style='color:#cc0000'>&#10007; HEALED LOCATOR ALSO FAILED</b><br>"
                    f"<b>Variable :</b> <code style='color:#9b59b6'>{var_name}</code><br>"
                    f"<b>Tried    :</b> <code>{healed_xpath}</code><br>"
                    f"<b>Error    :</b> {type(retry_exc).__name__}: "
                    f"{str(retry_exc)[:200]}"
                    f"</div>",
                    "WARN", html=True)
                unhealable_cache[value_str] = now + listener_ref.UNHEALABLE_TTL
                raise captured_error

        # ── Install patch ────────────────────────────────────────────────────
        healed_find_element._is_healed = True
        WebDriver.find_element = healed_find_element
        self.webdriver_patched = True

        # Log to RF report only — no console print here
        self._log(
            "<b style='color:green'>&#9889; SELF-HEAL ENGINE ACTIVE — "
            "WebDriver.find_element patched.</b>",
            "INFO", html=True)

    # ────────────────────────────────────────────────────────────────────────
    # Helpers
    # ────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _log(message: str, level: str = "INFO", html: bool = False):
        """Write to Robot Framework log. Never raises."""
        try:
            BuiltIn().log(message, level, html=html)
        except Exception:
            pass
