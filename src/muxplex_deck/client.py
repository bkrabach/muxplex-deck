"""HTTP client for the muxplex server's read/switch API used by the sidecar.

Wraps `httpx.Client` with the federation Bearer auth header, TLS
verification (certifi by default, or an explicit CA bundle), and typed
parsing of the handful of endpoints the sidecar needs. Distinct exceptions
(`AuthError`, `UnreachableError`) let `main.py`'s state machine tell "server
down" apart from "bad key" without inspecting httpx internals.

Endpoint shapes below were read directly from the muxplex server source
(muxplex/muxplex/main.py, views.py, settings.py), not guessed:

- GET  /api/sessions              -> [{"name": str, "snapshot": str,
                                        "bell": {"last_fired_at": float|None,
                                                 "seen_at": float|None,
                                                 "unseen_count": int}}, ...]
- GET  /api/state                 -> {"active_session": str|None,
                                       "active_view": str, ...}
                                       (active_view defaults to "all" server-side)
- GET  /api/settings               -> {"views": [{"name": str,
                                        "sessions": [str, ...]}, ...],
                                        "hidden_sessions": [str, ...],
                                        "sort_order": str, ...}
                                        (sensitive keys are redacted by the
                                        server; irrelevant to view resolution)
- POST /api/sessions/{name}/connect -> {"active_session": str, "ttyd_port": int}
                                        (404 if name is not a known session)

Auth: every request (except the public /api/instance-info, which we don't
use) must carry `Authorization: Bearer <federation_key>`; the server checks
it fresh per request and returns 401/403 on rejection.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

import httpx


class MuxplexError(Exception):
    """Base class for muxplex client errors."""


class UnreachableError(MuxplexError):
    """The server did not respond (connection refused, timeout, DNS, TLS)."""


class AuthError(MuxplexError):
    """The server rejected the federation key (401/403)."""


@dataclass(frozen=True)
class Bell:
    """A session's bell-alert sub-state, as returned by GET /api/sessions."""

    last_fired_at: float | None
    seen_at: float | None
    unseen_count: int

    @property
    def needs_attention(self) -> bool:
        """Canonical "needs attention" predicate, ported verbatim from the PWA.

        A session needs attention iff it has unseen bell events AND either
        they have never been seen, or the most recent fire is newer than the
        last time they were seen. The muxplex frontend uses this identical
        form in three places; ported here so the deck's bell indicator
        matches it exactly rather than approximating with `unseen_count > 0`
        alone (which would keep showing attention after the user has already
        acknowledged a bell that hasn't fired again).
        """
        return self.unseen_count > 0 and (
            self.seen_at is None
            or (self.last_fired_at is not None and self.last_fired_at > self.seen_at)
        )


@dataclass(frozen=True)
class Session:
    """One tmux session as returned by GET /api/sessions."""

    name: str
    snapshot: str
    bell: Bell


@dataclass(frozen=True)
class ServerState:
    """The subset of GET /api/state the sidecar cares about."""

    active_session: str | None
    active_view: str


@dataclass(frozen=True)
class View:
    """One user-defined view, as returned by GET /api/settings.

    `sessions` is the view's raw membership list from muxplex. Entries are
    often the canonical "device_id:name" form -- muxplex runs a background
    normalization cycle that rewrites bare-name entries this way
    unconditionally, even with no federation/remote instances configured
    (verified empirically against a live server). `.views.resolve_view`
    matches against both the bare name and this prefixed form; see its
    module docstring for the full rationale.
    """

    name: str
    sessions: frozenset[str]


@dataclass(frozen=True)
class Settings:
    """The subset of GET /api/settings the sidecar needs for view resolution."""

    views: tuple[View, ...]
    hidden_sessions: frozenset[str]
    sort_order: str


def _parse_bell(raw: dict) -> Bell:
    return Bell(
        last_fired_at=raw.get("last_fired_at"),
        seen_at=raw.get("seen_at"),
        unseen_count=int(raw.get("unseen_count", 0)),
    )


def _parse_session(raw: dict) -> Session:
    return Session(
        name=raw["name"],
        snapshot=raw.get("snapshot", ""),
        bell=_parse_bell(raw.get("bell", {})),
    )


class MuxplexClient:
    """Thin, typed HTTP client for the sidecar's read/switch operations.

    Usage:
        >>> with MuxplexClient("https://spark-1:8088", "secret") as client:
        ...     sessions = client.get_sessions()
    """

    def __init__(
        self,
        server_url: str,
        federation_key: str,
        ca_file: Path | None = None,
        timeout: float = 5.0,
    ) -> None:
        verify: bool | str = str(ca_file) if ca_file else True
        self._client = httpx.Client(
            base_url=server_url,
            headers={"Authorization": f"Bearer {federation_key}"},
            verify=verify,
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> MuxplexClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _request(self, method: str, path: str) -> httpx.Response:
        try:
            response = self._client.request(method, path)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (401, 403):
                raise AuthError(
                    f"{method} {path} returned {status} -- federation key rejected"
                ) from exc
            raise MuxplexError(
                f"{method} {path} returned {status}: {exc.response.text}"
            ) from exc
        except httpx.HTTPError as exc:
            raise UnreachableError(f"{method} {path} failed: {exc}") from exc
        return response

    def get_sessions(self) -> list[Session]:
        """GET /api/sessions -> list of sessions with name, snapshot, bell."""
        response = self._request("GET", "/api/sessions")
        return [_parse_session(item) for item in response.json()]

    def get_state(self) -> ServerState:
        """GET /api/state -> the subset of persistent state we render from."""
        response = self._request("GET", "/api/state")
        data = response.json()
        return ServerState(
            active_session=data.get("active_session"),
            active_view=data.get("active_view") or "all",
        )

    def get_settings(self) -> Settings:
        """GET /api/settings -> views, hidden_sessions, sort_order.

        The server redacts sensitive keys (federation_key, remote instance
        keys) in this response; irrelevant to view resolution, so ignored
        here.
        """
        response = self._request("GET", "/api/settings")
        data = response.json()
        views = tuple(
            View(
                name=item.get("name", ""),
                sessions=frozenset(item.get("sessions") or []),
            )
            for item in (data.get("views") or [])
        )
        return Settings(
            views=views,
            hidden_sessions=frozenset(data.get("hidden_sessions") or []),
            sort_order=data.get("sort_order", "manual"),
        )

    def connect_session(self, name: str) -> None:
        """POST /api/sessions/{name}/connect -- switch the active session.

        Raises `MuxplexError` (404 wrapped) if *name* is not a known session.
        """
        self._request("POST", f"/api/sessions/{name}/connect")
