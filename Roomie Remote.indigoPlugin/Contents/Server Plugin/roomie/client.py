"""HTTP client for the Roomie Remote Local Network Control API.

All responses use the envelope {"st": "success"|"fail"|"error", "da": ..., "co": ...}
which is parsed centrally in _request(). No indigo imports — a session
object (anything with .get/.post returning a response with .status_code
and .json()) can be injected for tests.
"""

from __future__ import annotations

from typing import Any, Optional


class RoomieError(Exception):
    """Base for all Roomie client errors."""


class RoomieUnreachable(RoomieError):
    """Network-level failure: the controller didn't answer (app backgrounded,
    device asleep, wrong host, timeout)."""


class RoomieRequestFailed(RoomieError):
    """The API answered but refused the request (envelope st="fail" or HTTP 4xx)."""

    def __init__(
        self,
        message: str,
        co: Optional[Any] = None,
        status_code: Optional[int] = None,
    ):
        super().__init__(message)
        self.co = co
        self.status_code = status_code


class RoomieServerError(RoomieError):
    """The API reported an internal problem (envelope st="error" or HTTP 5xx)."""


class RoomieClient:
    def __init__(
        self,
        host: str,
        port: int = 47147,
        timeout: float = 5.0,
        session: Optional[Any] = None,
    ):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        if session is None:
            import requests

            session = requests.Session()
        self._session = session

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/api/v1"

    def get_rooms(self) -> list[dict]:
        return self._request("GET", "/rooms")

    def get_activities(self) -> list[dict]:
        return self._request("GET", "/activities")

    def get_devices(self) -> list[dict]:
        return self._request("GET", "/devices")

    def run_activity(
        self,
        uuid: str,
        ts: Optional[str] = None,
        delay: Optional[float] = None,
    ) -> Any:
        payload: dict[str, Any] = {"au": uuid}
        if ts is not None:
            payload["ts"] = ts
        if delay is not None:
            payload["de"] = float(delay)
        return self._request("POST", "/runactivity", payload)

    def press(
        self,
        button: str,
        roomuuid: str,
        count: int = 1,
        digits: Optional[str] = None,
        action: Optional[str] = None,
        hold_ms: Optional[int] = None,
        activityuuid: Optional[str] = None,
    ) -> Any:
        """Universal Remote press. `action` is tap (default) | press | release |
        repeat; `hold_ms` auto-releases a `press` server-side; `activityuuid`
        overrides the room's current activity for button resolution."""
        payload: dict[str, Any] = {"button": button, "roomuuid": roomuuid}
        if count != 1:
            payload["count"] = int(count)
        if digits is not None:
            payload["digits"] = digits
        if action is not None:
            payload["action"] = action
        if hold_ms is not None:
            payload["hold_ms"] = int(hold_ms)
        if activityuuid is not None:
            payload["activityuuid"] = activityuuid
        return self._request("POST", "/remote/press", payload)

    def get_capabilities(
        self,
        roomuuid: Optional[str] = None,
        activityuuid: Optional[str] = None,
        roomname: Optional[str] = None,
    ) -> Any:
        """Button resolution for every lexicon button, scoped to the addressed
        room's current activity (or an explicit activity). 409 when the room
        is off."""
        params = {
            key: value
            for key, value in (
                ("roomuuid", roomuuid),
                ("activityuuid", activityuuid),
                ("roomname", roomname),
            )
            if value
        }
        return self._request("GET", "/remote/capabilities", params=params)

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Any:
        url = self.base_url + path
        try:
            if method == "GET" and params:
                response = self._session.get(url, params=params, timeout=self.timeout)
            elif method == "GET":
                response = self._session.get(url, timeout=self.timeout)
            else:
                response = self._session.post(url, json=payload, timeout=self.timeout)
        except Exception as exc:
            raise RoomieUnreachable(f"{method} {url}: {exc}") from exc

        if response.status_code >= 500:
            raise RoomieServerError(f"{method} {path}: HTTP {response.status_code}")
        if response.status_code >= 400:
            co = None
            try:
                co = response.json().get("co")
            except Exception:
                pass
            raise RoomieRequestFailed(
                f"{method} {path}: HTTP {response.status_code}",
                co=co,
                status_code=response.status_code,
            )

        try:
            envelope = response.json()
        except Exception as exc:
            raise RoomieServerError(f"{method} {path}: invalid JSON response") from exc

        st = envelope.get("st")
        if st == "success":
            return envelope.get("da")
        if st == "fail":
            raise RoomieRequestFailed(
                f"{method} {path}: request failed (co={envelope.get('co')})",
                co=envelope.get("co"),
            )
        raise RoomieServerError(f"{method} {path}: API error (st={st!r})")
