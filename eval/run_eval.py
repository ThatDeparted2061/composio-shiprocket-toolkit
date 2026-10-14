"""50 scripted agent tasks, run with and without the reliability layer.

Each task is what an ops agent would be asked to do ("ship order SO-1001 to
560034", "cancel SO-2003", "where is AWB 1410...?"), executed as the tool calls
an agent would make, against the Shiprocket simulator with production-like
trouble: 12% throttling, 6% 5xx, 2% timeouts, 4% of writes losing their
response, and tokens that expire every few seconds (a day, compressed).

The scripted agent behaves the same in both runs, like a reasonable LLM agent:
it follows an error's ``action`` when there is one (wait and retry, fix the
named input, or stop), and when the error is unclassified it tries again once.
Only the client underneath changes: the bare client makes one attempt per
call, never refreshes its token and returns raw errors.

Success is judged from the simulator's final state, not from what the agent
says: a shipped order must exist exactly once with an AWB and a pickup.

    python -m eval.run_eval            # writes eval/results.json and eval/RESULTS.md
"""

from __future__ import annotations

import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from composio_shiprocket import ShiprocketClient, run  # noqa: E402
from mock.shiprocket_sim import Faults, serve  # noqa: E402

TRANSIENT = dict(rate_limited=0.12, server_error=0.06, timeout=0.02, lost_response=0.04, stall=1.2)
SCENARIOS = {
    "transient": dict(TRANSIENT, token_ttl=3600),  # tokens outlive the run
    "expiry": dict(TRANSIENT, token_ttl=2.0),  # and the token expires every 2 s, like a long-lived agent past 24 h
}
CITIES = [("560034", "Bengaluru", "Karnataka"), ("400076", "Mumbai", "Maharashtra"), ("110017", "New Delhi", "Delhi"),
          ("600042", "Chennai", "Tamil Nadu"), ("700091", "Kolkata", "West Bengal"), ("411014", "Pune", "Maharashtra")]


def tasks(seed: int = 11) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for i in range(28):
        out.append({"kind": "ship", "ref": f"SO-{1000 + i}", "city": rng.choice(CITIES), "kg": round(rng.uniform(0.2, 4), 2)})
    for i in range(6):  # the user names a pickup address that doesn't exist; the error says which ones do
        out.append({"kind": "ship_fix_input", "ref": f"SO-{2000 + i}", "city": rng.choice(CITIES), "kg": 1.0, "pickup": "Main Warehouse"})
    for i in range(8):
        out.append({"kind": "cancel", "ref": f"SO-{3000 + i}", "city": rng.choice(CITIES), "kg": 0.5})
    for i in range(4):
        out.append({"kind": "unserviceable", "ref": f"SO-{4000 + i}", "pincode": rng.choice(["799001", "737101", "194101"])})
    for i in range(4):
        out.append({"kind": "track_unknown", "awb": f"99{rng.randrange(10**9):09d}"})
    return out


class Agent:
    """Calls tools the way an agent loop does."""

    def __init__(self, client: ShiprocketClient):
        self.c = client
        self.calls = 0

    def call(self, tool: str, args: dict) -> dict:
        r: dict = {}
        unclassified_retry = True
        for _ in range(5):
            self.calls += 1
            r = run(self.c, tool, args)
            if r["successful"]:
                return r
            action = r.get("action")
            if action == "retry_later":
                time.sleep(min(float(r.get("retry_after_seconds") or 0.05), 0.2))
                continue
            if action == "fix_input":
                fixed = fix(args, r["error"])
                if fixed is None:
                    return r
                args = fixed
                continue
            if action is None and unclassified_retry:  # raw error: can't tell transient from permanent
                unclassified_retry = False
                fixed = fix(args, r["error"])
                args = fixed or args
                continue
            return r
        return r


def fix(args: dict, message: str) -> dict | None:
    """What a model does with 'Wrong Pickup location entered. Use one of: Primary, ...'."""
    if "Use one of:" in message and "pickup_location" in args:
        choice = message.split("Use one of:")[1].split(",")[0].strip().strip('"}]) ')
        return {**args, "pickup_location": choice}
    return None


def order_args(t: dict) -> dict:
    pin, city, state = t["city"]
    return {"order_id": t["ref"], "pickup_location": t.get("pickup", "Primary"), "customer_name": "Asha Rao",
            "address": "12 MG Road", "city": city, "pincode": pin, "state": state, "email": "asha@example.com",
            "phone": "9876543210", "items": [{"name": "Ceramic mug", "sku": "MUG-01", "units": 2, "selling_price": 349}],
            "length_cm": 20, "breadth_cm": 15, "height_cm": 10, "weight_kg": t["kg"]}


def perform(agent: Agent, t: dict) -> dict:
    """Run one task; returns what the agent reports."""
    k = t["kind"]
    if k in ("ship", "ship_fix_input"):
        pin = t["city"][0]
        s = agent.call("CHECK_SERVICEABILITY", {"pickup_postcode": "560001", "delivery_postcode": pin, "weight_kg": t["kg"]})
        if not s["successful"]:
            return {"ok": False, "step": "serviceability", "error": s}
        o = agent.call("CREATE_ORDER", order_args(t))
        if not o["successful"]:
            return {"ok": False, "step": "create", "error": o}
        sid = o["data"]["shipment_id"]
        aw = agent.call("ASSIGN_AWB", {"shipment_id": sid, "courier_id": s["data"]["cheapest_courier_id"]})
        if not aw["successful"]:
            return {"ok": False, "step": "awb", "error": aw}
        p = agent.call("SCHEDULE_PICKUP", {"shipment_ids": [sid]})
        if not p["successful"]:
            return {"ok": False, "step": "pickup", "error": p}
        tr = agent.call("TRACK_SHIPMENT", {"awb_code": aw["data"]["awb_code"]})
        if not tr["successful"]:
            return {"ok": False, "step": "track", "error": tr}
        return {"ok": True, "awb": aw["data"]["awb_code"], "status": tr["data"]["status"]}
    if k == "cancel":
        o = agent.call("CREATE_ORDER", order_args(t))
        if not o["successful"]:
            return {"ok": False, "step": "create", "error": o}
        x = agent.call("CANCEL_ORDERS", {"order_ids": [o["data"]["order_id"]]})
        if not x["successful"]:
            return {"ok": False, "step": "cancel", "error": x}
        g = agent.call("GET_ORDER", {"order_id": o["data"]["order_id"]})
        return {"ok": g["successful"], "status": g.get("data", {}).get("status"), "error": None if g["successful"] else g}
    if k == "unserviceable":
        s = agent.call("CHECK_SERVICEABILITY", {"pickup_postcode": "560001", "delivery_postcode": t["pincode"], "weight_kg": 1})
        return {"ok": True, "reported_unserviceable": (not s["successful"]) and "no courier" in s.get("error", "")}
    if k == "track_unknown":
        tr = agent.call("TRACK_SHIPMENT", {"awb_code": t["awb"]})
        return {"ok": True, "reported_not_found": (not tr["successful"]) and tr.get("action") == "not_found"}
    raise ValueError(k)


def judge(t: dict, report: dict, orders: list[dict]) -> tuple[bool, str]:
    """Was the task actually done? Decided from the simulator's state."""
    k = t["kind"]
    mine = [o for o in orders if o["channel_order_id"] == t.get("ref")]
    if k in ("ship", "ship_fix_input"):
        if len(mine) != 1:
            return False, f"{len(mine)} orders exist"
        o = mine[0]
        if not (o["awb"] and o["pickup"] and report.get("ok")):
            return False, f"stopped at {report.get('step', '?')}: {short(report)}"
        return True, ""
    if k == "cancel":
        ok = len(mine) == 1 and mine[0]["status"] == "CANCELED" and report.get("ok")
        return ok, "" if ok else f"stopped at {report.get('step', '?')}: {short(report)}"
    if k == "unserviceable":
        ok = not mine and report.get("reported_unserviceable")
        return ok, "" if ok else "did not report the pincode as unserviceable"
    ok = report.get("reported_not_found")
    return ok, "" if ok else "did not report the unknown AWB as not found"


def short(report: dict) -> str:
    e = report.get("error") or {}
    return (e.get("action") or "unclassified") + ": " + str(e.get("error", ""))[:80] if e else ""


def run_mode(resilient: bool, seed: int, scenario: str) -> dict:
    srv, state = serve(0, Faults(**SCENARIOS[scenario]), seed=seed)
    base = f"http://127.0.0.1:{srv.server_port}"
    client = ShiprocketClient("api-user@example.com", "s3cret", base, resilient=resilient, timeout=1.0,
                              base_delay=0.02, max_delay=0.3, rng=random.Random(seed))
    agent = Agent(client)
    results, start = [], time.time()
    for t in tasks():
        report = perform(agent, t)
        results.append((t, report))
    orders = httpx.get(base + "/admin/state").json()["orders"]
    judged = []
    for t, report in results:
        ok, why = judge(t, report, orders)
        judged.append({"kind": t["kind"], "ref": t.get("ref") or t.get("awb"), "ok": ok, "why": why})
    counters = httpx.get(base + "/admin/state").json()["counters"]
    srv.shutdown()
    client.close()
    return {"scenario": scenario, "resilient": resilient, "seed": seed, "succeeded": sum(j["ok"] for j in judged), "tasks": len(judged),
            "seconds": round(time.time() - start, 1), "tool_calls": agent.calls, "client": client.stats, "server": counters,
            "by_kind": {k: f"{sum(j['ok'] for j in judged if j['kind'] == k)}/{sum(1 for j in judged if j['kind'] == k)}" for k in dict.fromkeys(j["kind"] for j in judged)},
            "failures": Counter(j["why"].split(":")[0] for j in judged if not j["ok"]).most_common(),
            "results": judged}


def main():
    seeds = [1, 2, 3]
    runs = [run_mode(r, s, sc) for sc in SCENARIOS for s in seeds for r in (False, True)]
    out = Path(__file__).parent
    (out / "results.json").write_text(json.dumps(runs, indent=2))
    get = lambda sc, s, r: next(x for x in runs if x["scenario"] == sc and x["seed"] == s and x["resilient"] == r)  # noqa: E731
    mean = lambda sc, r: sum(get(sc, s, r)["succeeded"] for s in seeds) / len(seeds) / 50  # noqa: E731
    rows = ["| Seed | Transient faults: without | with | + token expiry: without | with |", "|---:|---:|---:|---:|---:|"]
    for s in seeds:
        rows.append(f"| {s} | {get('transient', s, False)['succeeded']}/50 | **{get('transient', s, True)['succeeded']}/50** "
                    f"| {get('expiry', s, False)['succeeded']}/50 | **{get('expiry', s, True)['succeeded']}/50** |")
    rows.append(f"| **Mean** | {mean('transient', False):.0%} | **{mean('transient', True):.0%}** "
                f"| {mean('expiry', False):.0%} | **{mean('expiry', True):.0%}** |")
    a, b = get("transient", 1, False), get("transient", 1, True)
    detail = ["", "By task kind (transient faults, seed 1):", "", "| Task | Without | With |", "|---|---:|---:|"]
    detail += [f"| {k} | {a['by_kind'][k]} | {b['by_kind'][k]} |" for k in b["by_kind"]]
    e = get("expiry", 1, True)
    detail += ["",
               f"Faults injected (transient, seed 1, with the layer): {b['server']}",
               f"Layer activity: {b['client']['retries']} retries across {b['client']['calls']} calls.",
               f"With token expiry (seed 1): {e['client']['refreshes']} token refreshes, {e['server']['expired_token']} requests hit an expired token.",
               f"Why runs without the layer failed (transient, seed 1): {dict(a['failures'])}"]
    md = "\n".join(rows + detail) + "\n"
    (out / "RESULTS.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
