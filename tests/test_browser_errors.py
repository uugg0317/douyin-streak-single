"""Pure exception classification; no application imports or live browser."""
import unittest
from core.browser_errors import BrowserSessionUnavailable, raise_if_browser_unavailable


class BrowserErrorTests(unittest.TestCase):
    def test_stopped_playwright_loop_is_fatal(self):
        with self.assertRaises(BrowserSessionUnavailable):
            raise_if_browser_unavailable(RuntimeError('Event loop is closed! Is Playwright already stopped?'))

    def test_closed_page_context_browser_and_driver_are_fatal(self):
        messages = ['Locator.count: Target page, context or browser has been closed',
                    'Browser has been closed', 'Connection closed while reading from the driver']
        for message in messages:
            with self.subTest(message=message), self.assertRaises(BrowserSessionUnavailable):
                raise_if_browser_unavailable(RuntimeError(message))

    def test_target_closed_type_without_text_is_recognized(self):
        TargetClosedError = type('TargetClosedError', (Exception,), {})
        with self.assertRaises(BrowserSessionUnavailable):
            raise_if_browser_unavailable(TargetClosedError())

    def test_regular_lookup_and_navigation_failures_are_not_reclassified(self):
        for message in ['Timeout 5000ms exceeded', 'strict mode violation',
                        'Execution context was destroyed, most likely because of a navigation',
                        'Frame was detached', 'net::ERR_CONNECTION_RESET', 'No matching contact']:
            with self.subTest(message=message):
                self.assertIsNone(raise_if_browser_unavailable(RuntimeError(message)))

    def test_private_driver_details_do_not_enter_public_error(self):
        raw = RuntimeError('Event loop is closed; https://private.example/?token=fixture-private-value')
        with self.assertRaises(BrowserSessionUnavailable) as caught:
            raise_if_browser_unavailable(raw)
        self.assertNotIn('fixture-private-value', str(caught.exception))
        self.assertNotIn('private.example', str(caught.exception))
        self.assertEqual(caught.exception.code, 'browser_session_closed')

    def test_wrapped_error_and_cycles_are_handled(self):
        outer = RuntimeError('worker failed')
        outer.__cause__ = RuntimeError('Event loop is closed')
        with self.assertRaises(BrowserSessionUnavailable):
            raise_if_browser_unavailable(outer)
        other = RuntimeError('not a driver error'); other.__cause__ = other
        self.assertIsNone(raise_if_browser_unavailable(other))

    def test_existing_public_exception_is_not_rewrapped(self):
        original = BrowserSessionUnavailable()
        with self.assertRaises(BrowserSessionUnavailable) as caught:
            raise_if_browser_unavailable(original)
        self.assertIs(original, caught.exception)


if __name__ == '__main__':
    unittest.main()
