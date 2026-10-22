"""Actual APScheduler DateTrigger arithmetic under simulated host timezones."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest import mock
from core import scheduler as s
from test_r08_retry_recovery import FakeScheduler


class ScheduleTimezoneTests(unittest.TestCase):
    def run_case(self, hours, instant, kind):
        host = timezone(timedelta(hours=hours))
        business = timezone(timedelta(hours=8))
        class HostDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = instant.astimezone(host if tz is None else tz)
                return value.replace(tzinfo=None) if tz is None else value
        scheduler = FakeScheduler()
        with mock.patch.object(s, '_scheduler', scheduler), \
             mock.patch.object(s, 'datetime', HostDatetime), \
             mock.patch.object(s, 'business_now', return_value=instant.astimezone(business), create=True), \
             mock.patch.object(s, 'load_config', return_value={'auto_run_enabled':True, 'jitter_minutes':1}), \
             mock.patch.object(s.random, 'uniform', return_value=30):
            if kind == 'retry':
                self.assertTrue(s.schedule_retry(lambda:None, delay_minutes=45))
                job=scheduler.jobs['retry_send']; expected=2700
            else:
                s._daily_job()
                job=scheduler.jobs['daily_send_jitter']; expected=30
        self.assertIsNotNone(job.next_run_time.utcoffset())
        self.assertEqual((job.next_run_time-instant).total_seconds(),expected)

    def test_retry_delay_is_absolute_across_midnight_and_host_offsets(self):
        for hours in [0,8,-5,-4]:
            for instant in [datetime(2026,10,18,23,50,tzinfo=timezone.utc),
                            datetime(2026,3,8,6,59,tzinfo=timezone.utc),
                            datetime(2026,3,8,7,1,tzinfo=timezone.utc)]:
                with self.subTest(host_offset=hours, instant=instant):
                    self.run_case(hours,instant,'retry')

    def test_jitter_delay_is_absolute_across_host_offsets(self):
        for hours in [0,8,-5,-4]:
            with self.subTest(host_offset=hours):
                self.run_case(hours,datetime(2026,10,18,15,59,50,tzinfo=timezone.utc),'jitter')


if __name__=='__main__':unittest.main()
