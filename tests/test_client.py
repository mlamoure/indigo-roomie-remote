"""Tests for RoomieClient: envelope parsing, error mapping, payloads.

HTTP is never touched — a FakeSession is injected into the client.
"""

import pytest

from roomie.client import (
    RoomieClient,
    RoomieRequestFailed,
    RoomieServerError,
    RoomieUnreachable,
)


class FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json = json_body

    def json(self):
        if self._json is None:
            raise ValueError("no JSON body")
        return self._json


class FakeSession:
    def __init__(self):
        self.responses = []
        self.calls = []

    def queue(self, response):
        self.responses.append(response)
        return self

    def _next(self):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, timeout=None):
        self.calls.append(("GET", url, None, timeout))
        return self._next()

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, json, timeout))
        return self._next()


def make_client(session, **kwargs):
    return RoomieClient("10.0.0.5", session=session, **kwargs)


def envelope(da, st="success", co=200):
    return FakeResponse(200, {"st": st, "da": da, "co": co})


class TestEnvelope:
    def test_success_returns_da(self):
        session = FakeSession().queue(envelope([{"roomuuid": "R1"}]))
        assert make_client(session).get_rooms() == [{"roomuuid": "R1"}]

    def test_fail_raises_request_failed_with_co(self):
        session = FakeSession().queue(
            FakeResponse(200, {"st": "fail", "da": None, "co": 404})
        )
        with pytest.raises(RoomieRequestFailed) as exc_info:
            make_client(session).get_rooms()
        assert exc_info.value.co == 404

    def test_error_raises_server_error(self):
        session = FakeSession().queue(FakeResponse(200, {"st": "error", "co": 500}))
        with pytest.raises(RoomieServerError):
            make_client(session).get_rooms()

    def test_invalid_json_raises_server_error(self):
        session = FakeSession().queue(FakeResponse(200, None))
        with pytest.raises(RoomieServerError):
            make_client(session).get_rooms()


class TestTransportErrors:
    def test_connection_error_raises_unreachable(self):
        session = FakeSession().queue(ConnectionError("refused"))
        with pytest.raises(RoomieUnreachable):
            make_client(session).get_rooms()

    def test_timeout_raises_unreachable(self):
        session = FakeSession().queue(TimeoutError("timed out"))
        with pytest.raises(RoomieUnreachable):
            make_client(session).get_rooms()


class TestHttpStatusMapping:
    @pytest.mark.parametrize("status", [404, 409, 422])
    def test_4xx_raises_request_failed_with_status(self, status):
        session = FakeSession().queue(FakeResponse(status, {"co": status}))
        with pytest.raises(RoomieRequestFailed) as exc_info:
            make_client(session).press("Play", "R1")
        assert exc_info.value.status_code == status
        assert exc_info.value.co == status

    def test_4xx_without_json_body(self):
        session = FakeSession().queue(FakeResponse(409, None))
        with pytest.raises(RoomieRequestFailed) as exc_info:
            make_client(session).press("ActivityOff", "R1")
        assert exc_info.value.status_code == 409
        assert exc_info.value.co is None

    def test_5xx_raises_server_error(self):
        session = FakeSession().queue(FakeResponse(500, None))
        with pytest.raises(RoomieServerError):
            make_client(session).get_rooms()


class TestPayloads:
    def test_url_construction(self):
        session = FakeSession().queue(envelope([]))
        RoomieClient("10.0.0.5", port=1234, session=session).get_rooms()
        assert session.calls[0][1] == "http://10.0.0.5:1234/api/v1/rooms"

    def test_timeout_passed_through(self):
        session = FakeSession().queue(envelope([]))
        RoomieClient("h", timeout=2.5, session=session).get_rooms()
        assert session.calls[0][3] == 2.5

    def test_run_activity_minimal(self):
        session = FakeSession().queue(envelope(None))
        make_client(session).run_activity("UUID-1")
        assert session.calls[0][2] == {"au": "UUID-1"}

    def test_run_activity_with_ts_and_delay(self):
        session = FakeSession().queue(envelope(None))
        make_client(session).run_activity("UUID-1+", ts="on", delay=2)
        assert session.calls[0][2] == {"au": "UUID-1+", "ts": "on", "de": 2.0}

    def test_press_minimal_omits_defaults(self):
        session = FakeSession().queue(envelope(None))
        make_client(session).press("Play", "ROOM-1")
        assert session.calls[0][2] == {"button": "Play", "roomuuid": "ROOM-1"}

    def test_press_with_count_and_digits(self):
        session = FakeSession().queue(envelope(None))
        make_client(session).press("Channel", "ROOM-1", count=3, digits="42")
        assert session.calls[0][2] == {
            "button": "Channel",
            "roomuuid": "ROOM-1",
            "count": 3,
            "digits": "42",
        }
