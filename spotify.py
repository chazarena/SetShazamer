"""Spotify Web API client for SetShazamer.

Authorization Code + PKCE against a loopback redirect, so no client secret
ships with the app: the user registers their own (free) Spotify developer app
and pastes its Client ID once. Tokens are cached in the per-user config dir
and refreshed automatically.

Endpoints follow the February 2026 Web API changes: ``POST /me/playlists`` to
create, ``/playlists/{id}/items`` to read and add, ``GET /search`` capped at
10 results.

Everything here is blocking (urllib); the GUI calls it from a worker thread.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable

from scanner import TrackEntry, artist_names, normalize_title

log = logging.getLogger("setshazamer.spotify")

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
DASHBOARD_URL = "https://developer.spotify.com/dashboard"

REDIRECT_HOST = "127.0.0.1"  # Spotify rejects "localhost"; loopback IPs only.
REDIRECT_PORT = 8888
REDIRECT_PATH = "/callback"
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}{REDIRECT_PATH}"

SCOPES = (
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
)

AUTH_TIMEOUT_SECONDS = 300
HTTP_TIMEOUT_SECONDS = 20
MAX_ATTEMPTS = 4
SEARCH_LIMIT = 10  # API maximum since Feb 2026.
ADD_BATCH = 100  # API maximum per request.

SETUP_STEPS = (
    "1. Open the Spotify Developer Dashboard and click \"Create app\".\n"
    "2. Any name/description. Under Redirect URIs add exactly:\n"
    f"      {REDIRECT_URI}\n"
    "3. Tick \"Web API\", save, then copy the app's Client ID here.\n"
    "4. Under User Management, add the Spotify account you'll sign in with.\n\n"
    "Spotify requires the developer app's owner to have Premium."
)


class SpotifyError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SpotifyAuthError(SpotifyError):
    """Not signed in, or the stored grant is no longer valid."""


# --------------------------------------------------------------------------- #
# Config / token storage
# --------------------------------------------------------------------------- #


def config_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    path = base / "SetShazamer"
    path.mkdir(parents=True, exist_ok=True)
    return path


class SettingsStore:
    """Small JSON file, readable only by the current user."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_dir() / "spotify.json"
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        try:
            self._data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            self._data = {}

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def update(self, **values: Any) -> None:
        with self._lock:
            for key, value in values.items():
                if value is None:
                    self._data.pop(key, None)
                else:
                    self._data[key] = value
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
            os.replace(tmp, self.path)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Playlist:
    id: str
    name: str
    owner_id: str
    collaborative: bool
    public: bool

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Playlist":
        return cls(
            id=data["id"],
            name=data.get("name") or "(untitled)",
            owner_id=(data.get("owner") or {}).get("id", ""),
            collaborative=bool(data.get("collaborative")),
            public=bool(data.get("public")),
        )


@dataclass(frozen=True)
class SpotifyTrack:
    uri: str
    name: str
    artists: str
    url: str

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "SpotifyTrack":
        return cls(
            uri=data["uri"],
            name=data.get("name", ""),
            artists=", ".join(a.get("name", "") for a in data.get("artists") or []),
            url=(data.get("external_urls") or {}).get("spotify", ""),
        )


# --------------------------------------------------------------------------- #
# OAuth (Authorization Code + PKCE)
# --------------------------------------------------------------------------- #

_CALLBACK_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>SetShazamer</title>
<style>body{{font:16px -apple-system,system-ui,sans-serif;background:#121212;color:#eee;
display:grid;place-items:center;height:100vh;margin:0}}h1{{font-size:20px}}</style></head>
<body><div><h1>{title}</h1><p>{body}</p></div></body></html>"""


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict[str, str]  # set per server

    def do_GET(self) -> None:  # noqa: N802 (stdlib name)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != REDIRECT_PATH:
            self.send_error(404)
            return
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        self.server.result.update(params)  # type: ignore[attr-defined]
        ok = "code" in params
        page = _CALLBACK_PAGE.format(
            title="Connected to Spotify" if ok else "Spotify sign-in failed",
            body="You can close this tab and return to SetShazamer."
            if ok else f"Spotify said: {params.get('error', 'unknown error')}",
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log.debug("callback: " + format, *args)


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class SpotifyClient:
    def __init__(self, store: SettingsStore | None = None) -> None:
        self.store = store or SettingsStore()
        self._token_lock = threading.Lock()
        self._user: dict[str, Any] | None = None

    # ---- state ----------------------------------------------------------- #

    @property
    def client_id(self) -> str:
        return self.store.get("client_id", "") or ""

    def set_client_id(self, client_id: str) -> None:
        client_id = client_id.strip()
        if client_id != self.client_id:
            # Tokens belong to the old app.
            self.store.update(client_id=client_id, token=None)
            self._user = None

    @property
    def is_connected(self) -> bool:
        token = self.store.get("token") or {}
        return bool(self.client_id and token.get("refresh_token"))

    @property
    def display_name(self) -> str:
        return self.store.get("display_name", "") or ""

    def disconnect(self) -> None:
        self.store.update(token=None, display_name=None, user_id=None)
        self._user = None

    # ---- auth ------------------------------------------------------------ #

    def authorize(
        self,
        cancel: threading.Event | None = None,
        open_url: Callable[[str], Any] = webbrowser.open,
    ) -> str:
        """Run the browser sign-in and return the account's display name."""
        if not self.client_id:
            raise SpotifyAuthError("Add your Spotify Client ID first.")
        cancel = cancel or threading.Event()
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(16)

        try:
            server = http.server.HTTPServer((REDIRECT_HOST, REDIRECT_PORT), _CallbackHandler)
        except OSError as exc:
            raise SpotifyAuthError(
                f"Port {REDIRECT_PORT} is busy, so the sign-in callback can't be received. "
                "Close whatever is using it and try again."
            ) from exc
        server.result = {}  # type: ignore[attr-defined]
        server.timeout = 0.5
        try:
            query = urllib.parse.urlencode({
                "client_id": self.client_id,
                "response_type": "code",
                "redirect_uri": REDIRECT_URI,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
                "scope": " ".join(SCOPES),
                "state": state,
            })
            open_url(f"{AUTH_URL}?{query}")
            deadline = time.monotonic() + AUTH_TIMEOUT_SECONDS
            result: dict[str, str] = server.result  # type: ignore[attr-defined]
            while not result and not cancel.is_set():
                if time.monotonic() > deadline:
                    raise SpotifyAuthError("Timed out waiting for Spotify sign-in.")
                server.handle_request()
        finally:
            server.server_close()

        if cancel.is_set() and not result:
            raise SpotifyAuthError("Sign-in cancelled.")
        if result.get("state") != state:
            raise SpotifyAuthError("Sign-in response didn't match this request; try again.")
        if "code" not in result:
            error = result.get("error", "unknown error")
            hint = " Check the Redirect URI in your Spotify app." if "redirect" in error else ""
            raise SpotifyAuthError(f"Spotify sign-in failed: {error}.{hint}")

        self._store_token(self._token_request({
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": self.client_id,
            "code_verifier": verifier,
        }))
        user = self.me(refresh=True)
        return user.get("display_name") or user.get("id", "")

    def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        req = urllib.request.Request(
            TOKEN_URL,
            data=urllib.parse.urlencode(form).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            payload = _read_error(exc)
            detail = payload.get("error_description") or payload.get("error") or exc.reason
            if payload.get("error") == "invalid_grant":
                self.store.update(token=None)
                raise SpotifyAuthError("Spotify sign-in expired. Connect again.", exc.code) from exc
            if payload.get("error") == "invalid_client":
                raise SpotifyAuthError(f"Spotify rejected the Client ID: {detail}", exc.code) from exc
            raise SpotifyError(f"Spotify token request failed: {detail}", exc.code) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SpotifyError(f"Couldn't reach Spotify: {exc}") from exc

    def _store_token(self, payload: dict[str, Any]) -> None:
        previous = self.store.get("token") or {}
        self.store.update(token={
            "access_token": payload["access_token"],
            # Refresh responses may omit the refresh token; keep the old one then.
            "refresh_token": payload.get("refresh_token") or previous.get("refresh_token"),
            "expires_at": time.time() + int(payload.get("expires_in", 3600)),
        })

    def _access_token(self, force_refresh: bool = False) -> str:
        with self._token_lock:
            token = self.store.get("token") or {}
            if not token.get("refresh_token") or not self.client_id:
                raise SpotifyAuthError("Not connected to Spotify.")
            if force_refresh or time.time() > token.get("expires_at", 0) - 60:
                self._store_token(self._token_request({
                    "grant_type": "refresh_token",
                    "refresh_token": token["refresh_token"],
                    "client_id": self.client_id,
                }))
                token = self.store.get("token")
            return token["access_token"]

    # ---- HTTP ------------------------------------------------------------ #

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = path if path.startswith("https://") else f"{API_URL}{path}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        refreshed = False

        for attempt in range(MAX_ATTEMPTS):
            headers = {"Authorization": f"Bearer {self._access_token()}"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and not refreshed:
                    refreshed = True
                    self._access_token(force_refresh=True)
                    continue
                if exc.code == 429 or exc.code >= 500:
                    if attempt + 1 < MAX_ATTEMPTS:
                        retry_after = exc.headers.get("Retry-After") if exc.headers else None
                        delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                        log.info("Spotify %s on %s; retrying in %.0fs", exc.code, path, delay)
                        time.sleep(min(delay, 30))
                        continue
                raise _api_error(exc) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt + 1 < MAX_ATTEMPTS:
                    time.sleep(2 ** attempt)
                    continue
                raise SpotifyError(f"Couldn't reach Spotify: {exc}") from exc
        raise SpotifyError("Spotify request failed after retries.")

    def _paged(self, path: str, params: dict[str, Any]) -> Iterable[dict[str, Any]]:
        page = self._request("GET", path, params)
        while True:
            yield from page.get("items") or []
            next_url = page.get("next")
            if not next_url:
                return
            page = self._request("GET", next_url)

    # ---- API ------------------------------------------------------------- #

    def me(self, refresh: bool = False) -> dict[str, Any]:
        if self._user is None or refresh:
            self._user = self._request("GET", "/me")
            self.store.update(
                display_name=self._user.get("display_name") or self._user.get("id"),
                user_id=self._user.get("id"),
            )
        return self._user

    def playlists(self) -> list[Playlist]:
        """Playlists the signed-in user can add to (owned or collaborative)."""
        my_id = self.me().get("id", "")
        result = []
        for item in self._paged("/me/playlists", {"limit": 50}):
            if not item:
                continue
            playlist = Playlist.from_api(item)
            if playlist.owner_id == my_id or playlist.collaborative:
                result.append(playlist)
        return result

    def create_playlist(self, name: str, description: str = "", public: bool = False) -> Playlist:
        data = self._request("POST", "/me/playlists", body={
            "name": name.strip() or "SetShazamer",
            "description": description[:300],
            "public": public,
        })
        return Playlist.from_api(data)

    def playlist_uris(self, playlist_id: str) -> set[str]:
        uris: set[str] = set()
        for row in self._paged(f"/playlists/{playlist_id}/items", {"limit": 50}):
            # Feb 2026 renamed "track" to "item"; accept either.
            item = (row or {}).get("item") or (row or {}).get("track") or {}
            if item.get("uri"):
                uris.add(item["uri"])
        return uris

    def add_to_playlist(self, playlist_id: str, uris: list[str]) -> None:
        for start in range(0, len(uris), ADD_BATCH):
            self._request("POST", f"/playlists/{playlist_id}/items",
                          body={"uris": uris[start:start + ADD_BATCH]})

    def search_tracks(self, query: str, limit: int = SEARCH_LIMIT) -> list[SpotifyTrack]:
        data = self._request("GET", "/search", {
            "q": query, "type": "track", "limit": min(limit, SEARCH_LIMIT),
        })
        return [SpotifyTrack.from_api(t) for t in (data.get("tracks") or {}).get("items") or [] if t]

    def find_track(self, entry: TrackEntry) -> SpotifyTrack | None:
        """Best Spotify match for a Shazam result: ISRC, then fielded search, then free text."""
        if entry.spotify_uri.startswith("spotify:track:"):
            return SpotifyTrack(entry.spotify_uri, entry.title, entry.artist, "")

        if entry.isrc:
            hits = self.search_tracks(f"isrc:{entry.isrc}", limit=1)
            if hits:
                return hits[0]

        primary = _primary_artist(entry.artist)
        queries = [
            f'track:"{_quote_safe(entry.title)}" artist:"{_quote_safe(primary)}"',
            f"{primary} {normalize_title(entry.title)}",
        ]
        shazam_query = _shazam_search_query(entry.spotify_uri)
        if shazam_query:
            queries.append(shazam_query)

        best: tuple[float, SpotifyTrack] | None = None
        for query in queries:
            for track in self.search_tracks(query):
                score = match_score(entry, track)
                if best is None or score > best[0]:
                    best = (score, track)
            if best and best[0] >= 0.95:
                break
        if best and best[0] >= 0.75:
            return best[1]
        return None


# --------------------------------------------------------------------------- #
# Matching helpers
# --------------------------------------------------------------------------- #


def _primary_artist(artist: str) -> str:
    names = [part.strip() for part in artist.replace(" feat. ", ",").replace(" & ", ",").split(",")]
    return next((n for n in names if n), artist)


def _quote_safe(text: str) -> str:
    return text.replace('"', " ")


def _shazam_search_query(uri: str) -> str:
    # Shazam's hub hands out "spotify:search:<url-encoded artist + title>".
    if uri.startswith("spotify:search:"):
        return urllib.parse.unquote_plus(uri.split(":", 2)[2]).strip()
    return ""


def _clean_full(title: str) -> str:
    return " ".join(normalize_title(title.replace("(", " ").replace(")", " ")
                                   .replace("[", " ").replace("]", " ")).split())


def match_score(entry: TrackEntry, track: SpotifyTrack) -> float:
    """0..1. Title similarity dominates; artist overlap is required to score high."""
    base_a, base_b = normalize_title(entry.title), normalize_title(track.name)
    title = SequenceMatcher(None, base_a, base_b).ratio()
    # Same version (remix name etc.) beats a bare base-title match.
    full = SequenceMatcher(None, _clean_full(entry.title), _clean_full(track.name)).ratio()

    mine, theirs = artist_names(entry.artist), artist_names(track.artists)
    if mine and theirs:
        overlap = len(mine & theirs) / len(mine | theirs)
        artist = max(overlap, SequenceMatcher(None, " ".join(sorted(mine)),
                                              " ".join(sorted(theirs))).ratio())
        if not mine & theirs:
            artist *= 0.8
    else:
        artist = 0.5
    return 0.5 * title + 0.15 * full + 0.35 * artist


def _read_error(exc: urllib.error.HTTPError) -> dict[str, Any]:
    try:
        payload = json.loads(exc.read() or b"{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _api_error(exc: urllib.error.HTTPError) -> SpotifyError:
    payload = _read_error(exc)
    error = payload.get("error")
    message = error.get("message") if isinstance(error, dict) else payload.get("error_description")
    message = message or str(exc.reason)
    if exc.code == 401:
        return SpotifyAuthError(f"Spotify sign-in is no longer valid ({message}). Connect again.", 401)
    if exc.code == 403:
        return SpotifyError(
            f"Spotify refused the request ({message}). In Development Mode the app owner needs "
            "Premium and your account must be listed under User Management in the dashboard.",
            403,
        )
    return SpotifyError(f"Spotify error {exc.code}: {message}", exc.code)
