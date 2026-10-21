"""Local, empty-profile Chromium tests; a missing executable is an explicit skip."""
from contextlib import contextmanager
from functools import lru_cache
import os
import unittest
from playwright.sync_api import sync_playwright, Error

SKIP_REASON = 'no_chromium_headless_shell'


class ChromiumUnavailable(RuntimeError):
    pass


@contextmanager
def managed_chromium():
    driver = sync_playwright().start()
    browser = None
    try:
        try:
            browser = driver.chromium.launch(headless=True, args=['--disable-background-networking','--disable-component-update'])
        except Error as error:
            if "Executable doesn't exist" in str(error):
                raise ChromiumUnavailable(SKIP_REASON) from None
            raise
        yield driver, browser
    finally:
        try:
            if browser is not None:
                browser.close()
        finally:
            driver.stop()


@lru_cache(maxsize=8)
def _probe(browser_path):
    try:
        with managed_chromium():
            return True
    except ChromiumUnavailable:
        return False


def chromium_available():
    return _probe(os.environ.get('PLAYWRIGHT_BROWSERS_PATH',''))


def requires_chromium(target):
    return unittest.skipUnless(chromium_available(), SKIP_REASON)(target)
