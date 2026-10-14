"""A Shiprocket API simulator for tests and the evaluation.

Serves the same paths as apiv2.shiprocket.in/v1/external (auth/login, courier
serviceability, orders/create/adhoc, courier/assign/awb, courier/generate/pickup,
courier/track/awb, orders/show, orders/cancel, orders search, shipments) with
the failure modes agents hit on real SaaS APIs:

- tokens that expire (24 h in production, ``token_ttl`` seconds here) -> 401
- 429 throttling and 502/503 errors, at configurable rates
- timeouts (the server stalls past the client's timeout)
- lost responses: a write that commits, then answers 502
- unserviceable pincodes (no courier) and validation errors

Run standalone:  python -m mock.shiprocket_sim --port 8765
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


@dataclass
class Faults:
    rate_limited: float = 0.0  # 429
    server_error: float = 0.0  # 502/503
    timeout: float = 0.0  # stall for `stall` seconds
    lost_response: float = 0.0  # writes only: commit, then 502
    stall: float = 1.5
    token_ttl: float = 24 * 3600


@dataclass
class State:
    faults: Faults = field(default_factory=Faults)
    seed: int = 7
    lock: threading.Lock = field(default_factory=threading.Lock)
    tokens: dict = field(default_factory=dict)  # token -> expiry (epoch seconds)
    orders: dict = field(default_factory=dict)  # shiprocket order id -> order
    by_channel: dict = field(default_factory=dict)  # channel order id -> shiprocket order id
    shipments: dict = field(default_factory=dict)  # shipment id -> order id
    awbs: dict = field(default_factory=dict)  # awb -> shipment id
    next_id: int = 100000
    counters: dict = field(default_factory=lambda: {"requests": 0, "429": 0, "5xx": 0, "timeouts": 0, "lost": 0, "expired_token": 0})

    def __post_init__(self):
        self.rng = random.Random(self.seed)


UNSERVICEABLE = {"799001", "737101", "194101"}  # remote pincodes: no courier
USERS = {"api-user@example.com": "s3cret"}


def make_token(ttl: float) -> tuple[str, float]:
    """A JWT-shaped token whose exp the client can read."""
    exp = time.time() + ttl
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp, "iss": "sim"}).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJIUzI1NiJ9.{payload}.{random.getrandbits(64):x}", exp


def handler(state: State):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # quiet
            pass

        def send(self, code: int, body: dict, headers: dict | None = None):
            b = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):
            self.route("GET")

        def do_POST(self):
            self.route("POST")

        def route(self, method: str):
            u = urlparse(self.path)
            path, q = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
            if path == "/admin/state":
                with state.lock:
                    return self.send(200, {"orders": list(state.orders.values()), "counters": state.counters})
            if path == "/admin/faults" and method == "POST":
                f = self.body()
                with state.lock:
                    for k, v in f.items():
                        setattr(state.faults, k, v)
                return self.send(200, {"ok": True})
            body = self.body() if method == "POST" else {}
            with state.lock:
                state.counters["requests"] += 1
                f = state.faults
                roll = state.rng.random()
            if path == "/v1/external/auth/login":
                if USERS.get(body.get("email")) != body.get("password"):
                    return self.send(400, {"message": "Invalid email and password combination", "status_code": 400})
                tok, exp = make_token(f.token_ttl)
                with state.lock:
                    state.tokens[tok] = exp
                return self.send(200, {"token": tok, "email": body["email"], "id": 1})
            # Auth
            tok = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            with state.lock:
                exp = state.tokens.get(tok)
            if exp is None or exp < time.time():
                with state.lock:
                    state.counters["expired_token"] += 1
                return self.send(401, {"message": "Token has expired" if exp else "Unauthenticated.", "status_code": 401})
            # Faults
            if roll < f.rate_limited:
                with state.lock:
                    state.counters["429"] += 1
                return self.send(429, {"message": "Too Many Attempts.", "status_code": 429})
            roll -= f.rate_limited
            if roll < f.server_error:
                with state.lock:
                    state.counters["5xx"] += 1
                return self.send(state.rng.choice([502, 503]), {"message": "Bad Gateway", "status_code": 502})
            roll -= f.server_error
            if roll < f.timeout:
                with state.lock:
                    state.counters["timeouts"] += 1
                time.sleep(f.stall)
                return self.send(504, {"message": "Gateway Timeout"})
            roll -= f.timeout
            lose = method == "POST" and roll < f.lost_response
            code, resp = self.api(method, path, q, body)
            if lose and code < 300:
                with state.lock:
                    state.counters["lost"] += 1
                return self.send(502, {"message": "Bad Gateway", "status_code": 502})
            return self.send(code, resp)

        def api(self, method, path, q, body):
            with state.lock:
                return dispatch(state, method, path, q, body)

    return H


def dispatch(s: State, method: str, path: str, q: dict, b: dict):
    if method == "GET" and path == "/v1/external/courier/serviceability/":
        for k in ("pickup_postcode", "delivery_postcode", "weight"):
            if k not in q:
                return 422, {"message": "The given data was invalid.", "errors": {k: [f"The {k} field is required."]}}
        if q["delivery_postcode"] in UNSERVICEABLE:
            return 200, {"status": 404, "data": {"available_courier_companies": []}, "message": "No courier serviceable"}
        w = float(q["weight"])
        couriers = [{"courier_company_id": cid, "courier_name": name, "rate": round(base + 18 * w, 2), "estimated_delivery_days": str(days)}
                    for cid, name, base, days in [(10, "Delhivery Surface", 62, 4), (32, "Xpressbees", 58, 5), (54, "Ekart", 71, 3), (24, "Blue Dart", 119, 2)]]
        return 200, {"status": 200, "data": {"available_courier_companies": couriers}}
    if method == "POST" and path == "/v1/external/orders/create/adhoc":
        errors = {}
        for k in ("order_id", "order_date", "pickup_location", "billing_customer_name", "billing_address", "billing_city",
                  "billing_pincode", "billing_state", "billing_country", "billing_email", "billing_phone", "order_items",
                  "payment_method", "sub_total", "length", "breadth", "height", "weight"):
            if b.get(k) in (None, "", []):
                errors[k] = [f"The {k} field is required."]
        if b.get("billing_phone") and not re.fullmatch(r"[6-9]\d{9}", str(b["billing_phone"])):
            errors["billing_phone"] = ["Please enter a valid 10 digit phone number."]
        if b.get("pickup_location") and b["pickup_location"] not in ("Primary", "Warehouse-BLR"):
            errors["pickup_location"] = ["Wrong Pickup location entered. Use one of: Primary, Warehouse-BLR"]
        if errors:
            return 422, {"message": "The given data was invalid.", "errors": errors}
        if b["order_id"] in s.by_channel:
            return 422, {"message": f"Order Id {b['order_id']} already exists", "status_code": 422}
        s.next_id += 1
        oid, sid = s.next_id, s.next_id + 500000
        s.orders[oid] = {"id": oid, "channel_order_id": b["order_id"], "status": "NEW", "shipment_id": sid, "awb": None,
                         "courier": None, "pickup": False, "pincode": b["billing_pincode"]}
        s.by_channel[b["order_id"]] = oid
        s.shipments[sid] = oid
        return 200, {"order_id": oid, "shipment_id": sid, "status": "NEW", "status_code": 1}
    if method == "GET" and path == "/v1/external/orders":
        term = q.get("search", "")
        return 200, {"data": [{"id": o["id"], "channel_order_id": o["channel_order_id"], "status": o["status"],
                               "shipments": [{"id": o["shipment_id"], "awb": o["awb"]}]} for o in s.orders.values() if o["channel_order_id"] == term]}
    if method == "POST" and path == "/v1/external/courier/assign/awb":
        oid = s.shipments.get(b.get("shipment_id"))
        if oid is None:
            return 422, {"message": "Invalid shipment id", "status_code": 422}
        o = s.orders[oid]
        if o["status"] == "CANCELED":
            return 400, {"message": "Order is cancelled", "status_code": 400}
        if o["awb"]:
            return 400, {"message": "AWB is already assigned", "status_code": 400}
        names = {10: "Delhivery Surface", 32: "Xpressbees", 54: "Ekart", 24: "Blue Dart"}
        cid = b.get("courier_id") or 32
        o["awb"], o["courier"], o["status"] = f"{1410000000 + oid}", names.get(cid, "Xpressbees"), "AWB ASSIGNED"
        s.awbs[o["awb"]] = o["shipment_id"]
        return 200, {"awb_assign_status": 1, "response": {"data": {"awb_code": o["awb"], "courier_name": o["courier"], "shipment_id": o["shipment_id"]}}}
    if method == "GET" and path.startswith("/v1/external/shipments/"):
        oid = s.shipments.get(int(path.rsplit("/", 1)[1]))
        if oid is None:
            return 404, {"message": "Shipment not found"}
        o = s.orders[oid]
        return 200, {"data": {"id": o["shipment_id"], "awb": o["awb"], "courier": o["courier"], "status": o["status"]}}
    if method == "POST" and path == "/v1/external/courier/generate/pickup":
        ids = b.get("shipment_id") or []
        for sid in ids:
            oid = s.shipments.get(sid)
            if oid is None or not s.orders[oid]["awb"]:
                return 400, {"message": f"Shipment {sid} has no AWB assigned yet", "status_code": 400}
            if s.orders[oid]["pickup"]:
                return 400, {"message": "Already in Pickup Queue.", "status_code": 400}
        for sid in ids:
            o = s.orders[s.shipments[sid]]
            o["pickup"], o["status"] = True, "PICKUP SCHEDULED"
        return 200, {"pickup_status": 1, "response": {"pickup_scheduled_date": "2026-10-16 10:00:00", "pickup_token_number": f"Reference No: {ids[0]}"}}
    if method == "GET" and path.startswith("/v1/external/courier/track/awb/"):
        awb = path.rsplit("/", 1)[1]
        sid = s.awbs.get(awb)
        if sid is None:
            return 200, {"tracking_data": {"track_status": 0, "error": f"Aahh! There is no activities found in our DB for AWB {awb}"}}
        o = s.orders[s.shipments[sid]]
        return 200, {"tracking_data": {"track_status": 1, "shipment_status": 6, "etd": "2026-10-19",
                                       "shipment_track": [{"awb_code": awb, "current_status": o["status"]}],
                                       "shipment_track_activities": [{"date": "2026-10-15 09:00:00", "status": "X-UCI", "activity": "Manifested", "location": "Bengaluru"}]}}
    if method == "GET" and path.startswith("/v1/external/orders/show/"):
        o = s.orders.get(int(path.rsplit("/", 1)[1]))
        if o is None:
            return 404, {"message": "Order not found."}
        return 200, {"data": {"id": o["id"], "channel_order_id": o["channel_order_id"], "status": o["status"], "shipments": {"id": o["shipment_id"], "awb": o["awb"]}}}
    if method == "POST" and path == "/v1/external/orders/cancel":
        ids = b.get("ids") or []
        missing = [i for i in ids if i not in s.orders]
        if missing:
            return 404, {"message": f"Order(s) {missing} not found"}
        for i in ids:
            if s.orders[i]["pickup"]:
                return 400, {"message": f"Order {i} is already picked up and cannot be cancelled"}
        for i in ids:
            s.orders[i]["status"] = "CANCELED"
        return 200, {"message": "Order cancelled successfully."}
    return 404, {"message": f"No route for {method} {path}"}


def serve(port: int = 0, faults: Faults | None = None, seed: int = 7):
    """Start the simulator in a background thread; returns (server, state)."""
    state = State(faults=faults or Faults(), seed=seed)
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler(state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    srv, _ = serve(args.port)
    print(f"Shiprocket simulator on http://127.0.0.1:{srv.server_port}")
    threading.Event().wait()
