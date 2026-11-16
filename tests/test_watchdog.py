"""
Tests for the uptime watchdog's policy.

What can go wrong with a watchdog is not usually the HTTP request. It is the
bookkeeping around it: mailing on every check until the mailbox is unusable,
mailing once and never again so a week of downtime passes quietly, or never
saying it came back. So these tests drive `decide` -- the one function with the
policy in it -- through the sequences that matter, and leave the network and
the mailbox alone.

`deploy/watchdog.py` is a script rather than part of a package, and
deliberately imports nothing of the app's, so it is loaded here by path.
"""

import datetime
import importlib.util
import json
import os

import pytest

WATCHDOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "deploy",
    "watchdog.py",
)


def load_watchdog():
    spec = importlib.util.spec_from_file_location("newfire_watchdog", WATCHDOG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


watchdog = load_watchdog()

REMINDER = datetime.timedelta(hours=24)
T0 = datetime.datetime(2026, 11, 5, 0, 41, 0)


def minutes(n):
    return T0 + datetime.timedelta(minutes=n)


def hours(n):
    return T0 + datetime.timedelta(hours=n)


@pytest.fixture
def up_state():
    """What the state file holds while all is well."""
    return {"status": "up", "since": None, "last_alert": None}


# ------------------------------------------------------------- transitions


def test_quiet_while_the_site_is_up(up_state):
    action, state = watchdog.decide(up_state, True, T0, REMINDER)
    assert action == "none"
    assert state["status"] == "up"


def test_first_failure_alerts_and_records_when_it_began(up_state):
    action, state = watchdog.decide(up_state, False, T0, REMINDER)
    assert action == "down"
    assert state["status"] == "down"
    assert state["since"] == T0.isoformat()
    assert state["last_alert"] == T0.isoformat()


def test_a_site_still_down_five_minutes_later_says_nothing(up_state):
    _, state = watchdog.decide(up_state, False, T0, REMINDER)
    action, state = watchdog.decide(state, False, minutes(5), REMINDER)
    assert action == "none"
    # The outage still began when it began, not at the most recent check.
    assert state["since"] == T0.isoformat()


def test_recovery_is_reported_once_then_silence(up_state):
    _, state = watchdog.decide(up_state, False, T0, REMINDER)
    action, state = watchdog.decide(state, True, hours(3), REMINDER)
    assert action == "recovered"
    assert state["status"] == "up"
    assert state["since"] is None

    action, state = watchdog.decide(state, True, hours(4), REMINDER)
    assert action == "none"


def test_a_long_outage_is_raised_again_every_reminder_interval(up_state):
    """The point of the reminder: this is the week that went unnoticed."""
    _, state = watchdog.decide(up_state, False, T0, REMINDER)
    alerts = 1
    # Five minutes at a time for eight days, as cron would.
    for step in range(1, 8 * 24 * 12 + 1):
        action, state = watchdog.decide(state, False, minutes(5 * step), REMINDER)
        if action == "still_down":
            alerts += 1
        else:
            assert action == "none"
    # One at the start and one a day after that, not 2,300 of them.
    assert alerts == 9
    assert state["since"] == T0.isoformat()


def test_the_reminder_interval_is_honoured_exactly(up_state):
    _, state = watchdog.decide(up_state, False, T0, REMINDER)
    action, _ = watchdog.decide(state, False, hours(23), REMINDER)
    assert action == "none"
    action, _ = watchdog.decide(state, False, hours(24), REMINDER)
    assert action == "still_down"


def test_a_flap_down_up_down_alerts_each_time_it_goes_down(up_state):
    action, state = watchdog.decide(up_state, False, T0, REMINDER)
    assert action == "down"
    action, state = watchdog.decide(state, True, minutes(10), REMINDER)
    assert action == "recovered"
    action, state = watchdog.decide(state, False, minutes(20), REMINDER)
    assert action == "down"
    assert state["since"] == minutes(20).isoformat()


def test_a_down_state_with_no_last_alert_alerts_rather_than_waiting():
    """A hand-edited or half-written state file must not silence the alert."""
    state = {"status": "down", "since": T0.isoformat(), "last_alert": None}
    action, _ = watchdog.decide(state, False, minutes(5), REMINDER)
    assert action == "still_down"


# ------------------------------------------------------------- state file


def test_a_missing_state_file_reads_as_up(tmp_path):
    state = watchdog.load_state(str(tmp_path / "nope.state"))
    assert state == {"status": "up", "since": None, "last_alert": None}


def test_a_corrupt_state_file_reads_as_up_rather_than_crashing(tmp_path):
    path = tmp_path / "watchdog.state"
    path.write_text("{not json at all")
    assert watchdog.load_state(str(path))["status"] == "up"


def test_a_state_file_with_nonsense_status_reads_as_up(tmp_path):
    path = tmp_path / "watchdog.state"
    path.write_text(json.dumps({"status": "sideways"}))
    assert watchdog.load_state(str(path))["status"] == "up"


def test_state_survives_a_round_trip(tmp_path):
    path = str(tmp_path / "sub" / "watchdog.state")
    _, state = watchdog.decide(
        {"status": "up", "since": None, "last_alert": None}, False, T0, REMINDER
    )
    watchdog.save_state(state, path)
    assert watchdog.load_state(path) == state


# ------------------------------------------------------------- odds and ends


@pytest.mark.parametrize(
    "delta,expected",
    [
        (datetime.timedelta(seconds=30), "under a minute"),
        (datetime.timedelta(minutes=5), "5m"),
        (datetime.timedelta(hours=3), "3h"),
        (datetime.timedelta(hours=3, minutes=12), "3h 12m"),
        (datetime.timedelta(days=7, hours=12), "7d 12h"),
        (datetime.timedelta(days=2), "2d"),
    ],
)
def test_durations_read_the_way_a_subject_line_wants(delta, expected):
    assert watchdog.describe_duration(delta) == expected


def test_a_page_that_is_not_this_app_counts_as_down(monkeypatch):
    """A 200 from something other than the app is not the site being up.

    DreamHost's proxy and py4web both have placeholder pages of their own, and
    either one answering 200 would otherwise read as healthy.
    """

    class FakeResponse:
        def __init__(self, body, code=200):
            self._body = body.encode()
            self._code = code

        def read(self, _size=None):
            return self._body

        def getcode(self):
            return self._code

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(
        watchdog.urllib.request,
        "urlopen",
        lambda *a, **k: FakeResponse("<title>Welcome to nginx</title>"),
    )
    result = watchdog.look_once("https://example.test/", "<title>new fire</title>")
    assert not result.ok
    assert "did not contain" in result.summary

    monkeypatch.setattr(
        watchdog.urllib.request,
        "urlopen",
        lambda *a, **k: FakeResponse("<title>new fire</title> ..."),
    )
    assert watchdog.look_once("https://example.test/", "<title>new fire</title>").ok


def test_config_refuses_to_run_without_a_recipient():
    class Settings:
        SMTP_SERVER = "smtp.example.test:465"
        SMTP_SENDER = "a@example.test"
        SMTP_LOGIN = "a@example.test:secret"

    with pytest.raises(watchdog.ConfigError, match="WATCHDOG_RECIPIENT"):
        watchdog.Config(Settings())


def test_config_refuses_a_login_without_a_colon():
    class Settings:
        WATCHDOG_RECIPIENT = "you@example.test"
        SMTP_SERVER = "smtp.example.test:465"
        SMTP_SENDER = "a@example.test"
        SMTP_LOGIN = "just-a-password"

    with pytest.raises(watchdog.ConfigError, match="colon"):
        watchdog.Config(Settings())


def test_config_takes_the_reminder_interval_from_settings():
    class Settings:
        WATCHDOG_RECIPIENT = "you@example.test"
        WATCHDOG_REMINDER_HOURS = 6
        SMTP_SERVER = "smtp.example.test:465"
        SMTP_SENDER = "a@example.test"
        SMTP_LOGIN = "a@example.test:secret"

    assert watchdog.Config(Settings()).reminder == datetime.timedelta(hours=6)
