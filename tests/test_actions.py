import httpx
import pytest

from composio_shiprocket import ShiprocketClient, run, tool_schemas
from mock.shiprocket_sim import Faults, serve


@pytest.fixture
def sim():
    srv, state = serve(0, Faults())
    yield f"http://127.0.0.1:{srv.server_port}", state
    srv.shutdown()


def client(base, **kw):
    return ShiprocketClient("api-user@example.com", "s3cret", base, base_delay=0.001, max_delay=0.01, timeout=1.0, **kw)


ORDER = {"order_id": "SO-1", "pickup_location": "Primary", "customer_name": "Asha Rao", "address": "12 MG Road",
         "city": "Bengaluru", "pincode": "560034", "state": "Karnataka", "email": "asha@example.com", "phone": "9876543210",
         "items": [{"name": "Mug", "sku": "MUG-01", "units": 2, "selling_price": 349}],
         "length_cm": 20, "breadth_cm": 15, "height_cm": 10, "weight_kg": 0.8}


def test_full_shipping_flow(sim):
    base, _ = sim
    c = client(base)
    s = run(c, "CHECK_SERVICEABILITY", {"pickup_postcode": "560001", "delivery_postcode": "560034", "weight_kg": 0.8})
    assert s["successful"] and s["data"]["cheapest_courier_id"] == 32
    o = run(c, "CREATE_ORDER", ORDER)["data"]
    a = run(c, "ASSIGN_AWB", {"shipment_id": o["shipment_id"], "courier_id": 32})["data"]
    assert run(c, "SCHEDULE_PICKUP", {"shipment_ids": [o["shipment_id"]]})["data"]["pickup_scheduled"]
    t = run(c, "TRACK_SHIPMENT", {"awb_code": a["awb_code"]})["data"]
    assert t["status"] == "PICKUP SCHEDULED"


def test_lost_responses_never_duplicate_an_order(sim):
    base, state = sim
    state.faults.lost_response = 1.0  # every write commits, then answers 502
    c = client(base)
    o = run(c, "CREATE_ORDER", ORDER)
    assert o["successful"] and o["data"]["already_existed"]
    a = run(c, "ASSIGN_AWB", {"shipment_id": o["data"]["shipment_id"]})
    assert a["successful"] and a["data"]["already_assigned"] and a["data"]["awb_code"]
    p = run(c, "SCHEDULE_PICKUP", {"shipment_ids": [o["data"]["shipment_id"]]})
    assert p["successful"] and p["data"]["already_scheduled"]
    orders = httpx.get(base + "/admin/state").json()["orders"]
    assert len(orders) == 1 and orders[0]["awb"] and orders[0]["pickup"]


def test_errors_tell_the_agent_what_to_do(sim):
    base, _ = sim
    c = client(base)
    r = run(c, "CREATE_ORDER", {**ORDER, "pickup_location": "Main Warehouse"})
    assert r["action"] == "fix_input" and "Use one of: Primary, Warehouse-BLR" in r["error"]
    r = run(c, "CHECK_SERVICEABILITY", {"pickup_postcode": "560001", "delivery_postcode": "799001", "weight_kg": 1})
    assert r["action"] == "fix_input" and "no courier" in r["error"]
    r = run(c, "TRACK_SHIPMENT", {"awb_code": "999999"})
    assert r["action"] == "not_found"
    r = run(c, "CREATE_ORDER", {**ORDER, "phone": "12345"})  # rejected before any HTTP call
    assert r["action"] == "fix_input" and "phone" in r["error"]


def test_schemas_cover_every_tool():
    names = {t["name"] for t in tool_schemas()}
    assert names == {f"SHIPROCKET_{n}" for n in ("CHECK_SERVICEABILITY", "CREATE_ORDER", "ASSIGN_AWB", "SCHEDULE_PICKUP",
                                                  "TRACK_SHIPMENT", "GET_ORDER", "CANCEL_ORDERS")}
    assert all(t["description"] and t["input_schema"]["properties"] for t in tool_schemas())
