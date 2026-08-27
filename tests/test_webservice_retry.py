"""
Backoff arithmetic for the web service.

`_retry_after` is pure and takes only a response's headers, so it tests
directly -- no HTTP, no rate limiter, no source. What it protects is the
floor: a throttled service must never be retried sooner than our own
exponential delay would have, whatever it says in the header.
"""

from musicbrainz.webservice import WebServiceSource


class _Response:
    """The only part of a requests.Response that _retry_after reads."""

    def __init__(self, headers):
        self.headers = headers


def test_a_longer_retry_after_wins():
    # A server asking for more time gets it.
    assert WebServiceSource._retry_after(_Response({"Retry-After": "20"}), 2.0) == 20.0


def test_retry_after_zero_does_not_disable_the_backoff():
    # Observed from MusicBrainz on a 503. Honouring it literally would retry
    # immediately, which is the opposite of what a 503 is asking for.
    assert WebServiceSource._retry_after(_Response({"Retry-After": "0"}), 4.0) == 4.0


def test_a_shorter_retry_after_does_not_lower_the_floor():
    assert WebServiceSource._retry_after(_Response({"Retry-After": "1"}), 8.0) == 8.0


def test_no_header_falls_back():
    assert WebServiceSource._retry_after(_Response({}), 8.0) == 8.0


def test_a_date_header_falls_back():
    # Retry-After may be an HTTP date. isdigit() rejects it, and rather than
    # parse it we keep our own delay.
    response = _Response({"Retry-After": "Wed, 26 Aug 2026 21:00:00 GMT"})
    assert WebServiceSource._retry_after(response, 3.0) == 3.0
