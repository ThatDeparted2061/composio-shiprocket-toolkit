"""A Shiprocket API client with the reliability layer agents need.

- **Auth with refresh.** Shiprocket issues a JWT from ``POST /v1/external/auth/login``
  (an API user's email and password). The client refreshes it before it expires
  (from the token's ``exp``, or a configured lifetime) and once more on any 401,
  since sources disagree on whether tokens last 24 or 240 hours.
- **Rate-limit-aware retries.** 429s and 5xx are retried with exponential backoff
  and full jitter, honouring ``Retry-After``; timeouts and dropped connections too.
- **Errors an agent can act on.** Everything that still fails raises
  ``ShiprocketError`` with an action: retry later, re-authenticate, fix input,
  not found, or give up.

``resilient=False`` turns all of that off (one attempt, no refresh, raw errors)
so the evaluation can measure what the layer is worth.
"""

from __future__ import annotations

import base64
import json
import random
import threading
import time
from typing import Any, Callable

import httpx

from .errors import Action, ShiprocketError, classify

DEFAULT_BASE_URL = "https://apiv2.shiprocket.in"


class ShiprocketClient:
    def __init__(
        self,
        email: str,
        password: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        resilient: bool = True,
        max_attempts: int = 6,
        base_delay: float = 0.2,
        max_delay: float = 8.0,
        timeout: float = 10.0,
        token_ttl: float = 24 * 3600,  # assume the shorter lifetime when the token has no exp
        refresh_margin: float = 300,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.email, self.password = email, password
        self.resilient = resilient
        self.max_attempts = max_attempts if resilient else 1
        self.base_delay, self.max_delay = base_delay, max_delay
        self.token_ttl, self.refresh_margin = token_ttl, refresh_margin
        self.sleep, self.clock = sleep, clock
        self.rng = rng or random.Random()
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)
        self._token: str | None = None
        self._expires = 0.0
        self._margin = 0.0
        self._lock = threading.Lock()
        self.stats = {"calls": 0, "attempts": 0, "retries": 0, "refreshes": 0, "logins": 0}

    # -- auth ---------------------------------------------------------------

    def _login(self) -> None:
        self.stats["logins"] += 1
        try:
            r = self.http.post("/v1/external/auth/login", json={"email": self.email, "password": self.password})
        except httpx.HTTPError as e:
            raise ShiprocketError(Action.RETRY_LATER, f"login failed: {e}") from e
        if r.status_code != 200:
            action = Action.REAUTHENTICATE if r.status_code in (400, 401, 403) else classify(r.status_code, r.text)
            raise ShiprocketError(action, f"login failed: HTTP {r.status_code}: {r.text[:200]}", status=r.status_code)
        token = r.json().get("token")
        if not token:
            raise ShiprocketError(Action.GIVE_UP, "login response had no token")
        now = self.clock()
        self._token = token
        self._expires = _jwt_exp(token) or now + self.token_ttl
        # Refresh `refresh_margin` early, but never more than a tenth of the token's life early.
        self._margin = min(self.refresh_margin, (self._expires - now) / 10)

    def _ensure_token(self, force: bool = False) -> str:
        with self._lock:
            stale = self._token is None or (self.resilient and self.clock() > self._expires - self._margin)
            if force or stale:
                if self._token is not None:
                    self.stats["refreshes"] += 1
                self._login()
            return self._token  # type: ignore[return-value]

    # -- requests -----------------------------------------------------------

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        cap = min(self.max_delay, self.base_delay * (2**attempt))
        delay = self.rng.uniform(0, cap)  # full jitter: concurrent agents don't retry in lockstep
        if retry_after is not None:
            delay = max(delay, retry_after)
        return delay

    def request(self, method: str, path: str, *, params: dict | None = None, json_body: Any = None) -> Any:
        """Send a request; returns the decoded JSON body."""
        self.stats["calls"] += 1
        refreshed = False
        last: ShiprocketError | None = None
        attempt = 0
        while attempt < self.max_attempts:
            token = self._ensure_token()
            self.stats["attempts"] += 1
            try:
                r = self.http.request(method, path, params=params, json=json_body, headers={"Authorization": f"Bearer {token}"})
            except httpx.TimeoutException as e:
                last = ShiprocketError(Action.RETRY_LATER, f"{method} {path} timed out")
                if not self.resilient:
                    raise RuntimeError(f"{method} {path}: timed out") from e
            except httpx.TransportError as e:
                last = ShiprocketError(Action.RETRY_LATER, f"{method} {path}: connection failed ({type(e).__name__})")
                if not self.resilient:
                    raise RuntimeError(f"{method} {path}: {e}") from e
            else:
                if 200 <= r.status_code < 300:
                    return r.json() if r.content else {}
                if not self.resilient:
                    raise RuntimeError(f"{method} {path}: HTTP {r.status_code}: {r.text[:200]}")
                if r.status_code == 401 and not refreshed:
                    refreshed = True
                    self._ensure_token(force=True)  # the token died early: one fresh login, not a retry
                    continue
                action = classify(r.status_code, r.text)
                last = ShiprocketError(action, _message(r), status=r.status_code, retry_after=_retry_after(r))
                if action is not Action.RETRY_LATER:
                    raise last
            attempt += 1
            if attempt < self.max_attempts:
                self.stats["retries"] += 1
                self.sleep(self._backoff(attempt - 1, last.retry_after if last else None))
        assert last is not None
        raise last

    def close(self) -> None:
        self.http.close()


def _retry_after(r: httpx.Response) -> float | None:
    v = r.headers.get("Retry-After")
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def _message(r: httpx.Response) -> str:
    """Shiprocket puts the useful part in "message" or in per-field "errors"."""
    try:
        body = r.json()
    except ValueError:
        return f"HTTP {r.status_code}: {r.text[:200]}"
    msg = body.get("message") or f"HTTP {r.status_code}"
    if isinstance(body.get("errors"), dict):
        details = "; ".join(f"{k}: {', '.join(v) if isinstance(v, list) else v}" for k, v in body["errors"].items())
        msg = f"{msg} ({details})"
    return msg


def _jwt_exp(token: str) -> float | None:
    """The exp claim of a JWT, without verifying it (we only need the time)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, KeyError, ValueError, TypeError):
        return None
