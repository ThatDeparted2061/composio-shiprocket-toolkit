import base64
import json

import httpx
import pytest

from composio_shiprocket import Action, ShiprocketClient, ShiprocketError


def jwt(exp: float) -> str:
    p = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"h.{p}.s"


class Fake:
    """A scripted Shiprocket: login always works; API responses come from a queue."""

    def __init__(self, responses, exp=10_000.0):
        self.responses = list(responses)
        self.exp = exp
        self.logins = 0
        self.seen = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/auth/login"):
            self.logins += 1
            return httpx.Response(200, json={"token": jwt(self.exp)})
        self.seen.append(req.headers["Authorization"])
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(fake, **kw):
    sleeps = []
    c = ShiprocketClient("u", "p", "http://sr", transport=httpx.MockTransport(fake), sleep=sleeps.append,
                         clock=kw.pop("clock", lambda: 0.0), **kw)
    return c, sleeps


def test_retries_429_honouring_retry_after():
    fake = Fake([httpx.Response(429, headers={"Retry-After": "3"}, json={"message": "Too Many Attempts."}),
                 httpx.Response(503, json={"message": "Bad Gateway"}),
                 httpx.Response(200, json={"ok": 1})])
    c, sleeps = client(fake)
    assert c.request("GET", "/x") == {"ok": 1}
    assert sleeps[0] >= 3 and len(sleeps) == 2
    assert c.stats["retries"] == 2


def test_timeouts_are_retried_then_classified():
    fake = Fake([httpx.ReadTimeout("slow")] * 3)
    c, _ = client(fake, max_attempts=3)
    with pytest.raises(ShiprocketError) as e:
        c.request("GET", "/x")
    assert e.value.action is Action.RETRY_LATER


def test_refreshes_before_expiry_and_once_on_401():
    now = [0.0]
    fake = Fake([httpx.Response(200, json={}), httpx.Response(200, json={}),
                 httpx.Response(401, json={"message": "Token has expired"}), httpx.Response(200, json={})], exp=1000)
    c, _ = client(fake, clock=lambda: now[0], refresh_margin=300)
    c.request("GET", "/a")
    now[0], fake.exp = 950, 5000  # inside the margin (a tenth of the token's life): log in again before the call
    c.request("GET", "/b")
    assert fake.logins == 2
    c.request("GET", "/c")  # 401 mid-life: one fresh login, then the call succeeds
    assert fake.logins == 3 and c.stats["refreshes"] == 2


def test_persistent_401_asks_to_reauthenticate():
    fake = Fake([httpx.Response(401, json={"message": "Unauthenticated."})] * 2)
    c, _ = client(fake)
    with pytest.raises(ShiprocketError) as e:
        c.request("GET", "/x")
    assert e.value.action is Action.REAUTHENTICATE


@pytest.mark.parametrize("status,action", [(422, Action.FIX_INPUT), (404, Action.NOT_FOUND), (403, Action.GIVE_UP)])
def test_client_errors_are_not_retried(status, action):
    fake = Fake([httpx.Response(status, json={"message": "nope", "errors": {"billing_phone": ["Please enter a valid 10 digit phone number."]}})])
    c, sleeps = client(fake)
    with pytest.raises(ShiprocketError) as e:
        c.request("POST", "/x", json_body={})
    assert e.value.action is action and not sleeps
    assert "billing_phone: Please enter a valid 10 digit phone number." in e.value.message


def test_bad_credentials_ask_to_reauthenticate():
    def bad(req):
        return httpx.Response(400, json={"message": "Invalid email and password combination"})
    c, _ = client(bad)
    with pytest.raises(ShiprocketError) as e:
        c.request("GET", "/x")
    assert e.value.action is Action.REAUTHENTICATE


def test_bare_mode_makes_one_attempt_with_raw_errors():
    fake = Fake([httpx.Response(429, json={"message": "Too Many Attempts."})])
    c, _ = client(fake, resilient=False)
    with pytest.raises(RuntimeError, match="HTTP 429"):
        c.request("GET", "/x")
