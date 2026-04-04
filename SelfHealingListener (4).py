"""
SelfHealingListener.py
======================
Robot Framework Listener (API v3) — Self-healing engine for Appium XPath locators.

╔══════════════════════════════════════════════════════════════════════════════╗
║  TO ENABLE HEALING IN SETUP / TEARDOWN — change these two lines:           ║
║      HEAL_IN_SETUP    = False   →   HEAL_IN_SETUP    = True                ║
║      HEAL_IN_TEARDOWN = False   →   HEAL_IN_TEARDOWN = True                ║
╚══════════════════════════════════════════════════════════════════════════════╝

SUPPRESSION RULES  (healing skipped when ANY rule matches)
----------------------------------------------------------
1. Run Keyword And Ignore Error / Continue On Failure / Return Status / Expect Error
2. Negative RF keywords  (Element Should Not Exist, Wait Until Element Is Not Visible…)
3. ANY keyword called with  status=FALSE  argument
4. Test Setup  phase  (HEAL_IN_SETUP  flag, default False)
5. Test Teardown phase (HEAL_IN_TEARDOWN flag, default False)
6. Empty locator value
7. Locator is inside the per-locator unhealable cache (rate-limit)

IMPORTANT DESIGN DECISION — "check elements displayed"
-------------------------------------------------------
This keyword is NOT in _NEGATIVE_KEYWORDS.
When called with  status=TRUE  → healing MUST fire (element is expected present).
When called with  status=FALSE → suppressed via rule 3 (status=FALSE arg check).
Putting it in _NEGATIVE_KEYWORDS would suppress BOTH cases — wrong.

Wait Until Keyword Succeeds is also NOT suppressed — it retries until success,
so healing must work inside it.
"""

import time
import traceback
from collections import defaultdict

from robot.libraries.BuiltIn import BuiltIn
from HealerLogic import find_healed_locator

# Printed exactly once across the entire run regardless of how many suite files
_ENGINE_BANNER_PRINTED = False


class SelfHealingListener:

    ROBOT_LISTENER_API_VERSION = 3

    # ╔══════════════════════════════════════════════════════════════╗
    # ║           ★  CONFIGURATION — EDIT HERE  ★                  ║
    # ╚══════════════════════════════════════════════════════════════╝

    HEAL_IN_SETUP     = False   # True → heal during Test Setup keywords
    HEAL_IN_TEARDOWN  = False   # True → heal during Test Teardown keywords
    UNHEALABLE_TTL    = 15.0    # seconds before a failed locator is retried
    PERSISTENT_FAIL_THRESHOLD = 3

    # ── Keywords that suppress healing ───────────────────────────────────────
    #
    # ONLY true "swallow-error" wrappers go here.
    #
    # DO NOT add:
    #   • "wait until keyword succeeds"   → it retries, healing should fire
    #   • "check elements displayed"      → status=FALSE already handled by
    #                                       the arg-check in _suppression_reason_for
    #
    _IGNORE_ERROR_KEYWORDS = frozenset({
        "run keyword and ignore error",
        "run keyword and continue on failure",
        "run keyword and return status",
        "run keyword and expect error",
    })

    # Pure RF keywords that always assert element ABSENCE (no arg needed)
    _NEGATIVE_KEYWORDS = frozenset({
        "element should not be visible",
        "element should not exist",
        "page should not contain element",
        "page should not contain",
        "wait until element is not visible",
        "wait until page does not contain element",
    })

    # ─────────────────────────────────────────────────────────────────────────

    def __init__(self):
        self.webdriver_patched        = False
        self.unhealable_cache: dict   = {}
        self.failure_counts: dict     = defaultdict(int)
        self._suppress_stack: list    = []
        self._in_setup                = False
        self._in_teardown             = False
        self._var_name_cache: dict    = {}   # {xpath: robot_var_name}

    # ── Listener hooks ────────────────────────────────────────────────────────

    def start_suite(self, data, result):
        global _ENGINE_BANNER_PRINTED
        if not _ENGINE_BANNER_PRINTED:
            _ENGINE_BANNER_PRINTED = True
            print(
                "\033[32m\033[1m[SELF-HEAL] Engine ACTIVE — "
                "WebDriver.find_element patched globally.\033[0m",
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
            self._in_setup    = False
        elif kw_type == 'teardown':
            self._in_teardown = False

    # ── Suppression helpers ───────────────────────────────────────────────────

    def _suppression_reason_for(self, kw_name: str, args_upper: list) -> str:
        """
        Return a suppression reason string, or None if healing should proceed.

        Rule priority:
        1. Explicit ignore-error wrappers (by keyword name)
        2. Pure negative-assertion RF keywords (by keyword name)
        3. Any keyword called with status=FALSE  (covers project keywords like
           'Check Elements Displayed  status=FALSE' without hardcoding the name)
        """
        if kw_name in self._IGNORE_ERROR_KEYWORDS:
            return f"ignore-error wrapper '{kw_name}'"
        if kw_name in self._NEGATIVE_KEYWORDS:
            return f"negative-assertion '{kw_name}'"
        # Check for status=FALSE or bare FALSE as a positional arg
        # Works for ANY keyword that signals "expect element to be absent"
        if "STATUS=FALSE" in args_upper or (
            "FALSE" in args_upper and "TRUE" not in args_upper
        ):
            return f"status=FALSE argument in '{kw_name}'"
        return None

    def _get_suppression(self):
        if self._suppress_stack:
            return True, self._suppress_stack[-1]
        if self._in_setup and not self.HEAL_IN_SETUP:
            return True, "Test Setup (set HEAL_IN_SETUP=True to enable)"
        if self._in_teardown and not self.HEAL_IN_TEARDOWN:
            return True, "Test Teardown (set HEAL_IN_TEARDOWN=True to enable)"
        return False, ""

    # ── Variable name resolver (cached) ──────────────────────────────────────

    def _find_variable_name(self, xpath_value: str) -> str:
        if xpath_value in self._var_name_cache:
            return self._var_name_cache[xpath_value]
        result = "<unknown variable>"
        try:
            variables = BuiltIn().get_variables()
            for var_name, var_val in variables.items():
                if isinstance(var_val, str) and var_val == xpath_value:
                    result = var_name
                    break
        except Exception:
            pass
        self._var_name_cache[xpath_value] = result
        return result

    # ── WebDriver patch ───────────────────────────────────────────────────────

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

            # STEP 1 — normal find (fast path)
            captured_error = None
            try:
                return original_find_element(driver_self, by, value)
            except Exception as exc:
                captured_error = exc   # copy before Python deletes it

            # STEP 2 — normalise
            value_str = str(value).strip() if value is not None else ""
            if not value_str:
                raise captured_error

            # STEP 3 — suppression check (no I/O, instant)
            suppressed, suppress_reason = listener_ref._get_suppression()
            if suppressed:
                failure_counts[value_str] += 1
                listener_ref._log(
                    f"<span style='color:#999'>[SELF-HEAL] SUPPRESSED — "
                    f"{suppress_reason} | "
                    f"<code>{value_str[:120]}</code></span>",
                    "INFO", html=True)
                raise captured_error

            # STEP 4 — rate-limit check (no I/O, instant)
            now = time.time()
            if unhealable_cache.get(value_str, 0) > now:
                failure_counts[value_str] += 1
                remaining = unhealable_cache[value_str] - now
                listener_ref._log(
                    f"<span style='color:#999'>[SELF-HEAL] RATE-LIMITED "
                    f"({remaining:.0f}s remaining) | "
                    f"<code>{value_str[:120]}</code></span>",
                    "INFO", html=True)
                raise captured_error

            # STEP 5 — count failure + resolve variable name (var lookup is cached)
            failure_counts[value_str] += 1
            fail_count = failure_counts[value_str]
            var_name   = listener_ref._find_variable_name(value_str)

            # STEP 6 — log the trigger
            is_persistent = fail_count >= listener_ref.PERSISTENT_FAIL_THRESHOLD
            bc = "#cc0000" if is_persistent else "#e67e00"
            bt = (f"PERSISTENT FAIL #{fail_count}"
                  if is_persistent else f"FAIL #{fail_count}")

            listener_ref._log(
                f"<div style='border:2px solid {bc};border-radius:4px;"
                f"padding:6px 10px;margin:4px 0;background:#fff8f0'>"
                f"<b style='color:{bc}'>&#9888; SELF-HEAL TRIGGERED</b> "
                f"<span style='background:{bc};color:white;padding:1px 6px;"
                f"border-radius:3px;font-size:11px'>{bt}</span><br>"
                f"<b>Variable :</b> <code style='color:#9b59b6'>{var_name}</code><br>"
                f"<b>XPath    :</b> <code>{value_str}</code><br>"
                f"<b>Error    :</b> {type(captured_error).__name__}: "
                f"{str(captured_error)[:200]}"
                f"</div>",
                "WARN", html=True)

            # STEP 7 — fetch page source (only reached when healing will actually run)
            try:
                page_source = driver_self.page_source
            except Exception as ps_exc:
                listener_ref._log(
                    f"[SELF-HEAL] Cannot get page_source for {var_name}: {ps_exc}",
                    "WARN")
                unhealable_cache[value_str] = now + listener_ref.UNHEALABLE_TTL
                raise captured_error

            # STEP 8 — run healer logic
            try:
                new_locator = find_healed_locator(value_str, page_source)
            except Exception:
                listener_ref._log(
                    f"[SELF-HEAL] HealerLogic exception for {var_name}:<br>"
                    f"<pre>{traceback.format_exc()}</pre>",
                    "WARN", html=True)
                unhealable_cache[value_str] = now + listener_ref.UNHEALABLE_TTL
                raise captured_error

            # STEP 9 — no match found
            if not new_locator:
                listener_ref._log(
                    f"<div style='border:2px solid #cc0000;border-radius:4px;"
                    f"padding:6px 10px;margin:4px 0;background:#fff0f0'>"
                    f"<b style='color:#cc0000'>&#10007; NO MATCHING XPATH FOUND</b><br>"
                    f"<b>Variable :</b> <code style='color:#9b59b6'>{var_name}</code><br>"
                    f"<b>XPath    :</b> <code>{value_str}</code><br>"
                    f"<b>Failures :</b> {fail_count}<br>"
                    f"<i>Causes: element absent | tokens too generic | "
                    f"mid-transition | resource-id renamed</i>"
                    f"</div>",
                    "WARN", html=True)
                unhealable_cache[value_str] = now + listener_ref.UNHEALABLE_TTL
                raise captured_error

            # STEP 10 — try healed locator
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

        # ── Install patch ─────────────────────────────────────────────────────
        healed_find_element._is_healed = True
        WebDriver.find_element = healed_find_element
        self.webdriver_patched = True
        self._log(
            "<b style='color:green'>&#9889; SELF-HEAL ENGINE ACTIVE — "
            "WebDriver.find_element patched.</b>",
            "INFO", html=True)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _log(message: str, level: str = "INFO", html: bool = False):
        try:
            BuiltIn().log(message, level, html=html)
        except Exception:
            pass
