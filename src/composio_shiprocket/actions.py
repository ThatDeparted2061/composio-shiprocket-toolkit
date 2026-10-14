"""The toolkit's actions: Shiprocket's core shipping flow.

check_serviceability -> create_order -> assign_awb -> schedule_pickup -> track_shipment,
plus get_order and cancel_orders. Inputs are Pydantic models whose field
descriptions become the JSON schema the model sees.

Writes are made safe to retry: a create, AWB assignment or pickup that already
happened (because an earlier attempt succeeded but its response was lost)
returns the existing result instead of an error.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from .client import ShiprocketClient
from .errors import Action, ShiprocketError

PINCODE = r"^[1-9][0-9]{5}$"


class CheckServiceabilityInput(BaseModel):
    pickup_postcode: str = Field(pattern=PINCODE, description="6-digit pincode the parcel ships from")
    delivery_postcode: str = Field(pattern=PINCODE, description="6-digit pincode the parcel ships to")
    weight_kg: float = Field(gt=0, le=100, description="Parcel weight in kilograms")
    cod: bool = Field(False, description="True for cash on delivery")


class Item(BaseModel):
    name: str
    sku: str
    units: int = Field(ge=1)
    selling_price: float = Field(gt=0, description="Price per unit in INR")


class CreateOrderInput(BaseModel):
    order_id: str = Field(min_length=1, max_length=50, description="Your own unique reference for the order (makes retries safe)")
    order_date: str = Field(default_factory=lambda: date.today().isoformat(), description="YYYY-MM-DD")
    pickup_location: str = Field(description="Nickname of a pickup address saved in Shiprocket, e.g. 'Primary'")
    customer_name: str
    address: str
    city: str
    pincode: str = Field(pattern=PINCODE)
    state: str
    email: str
    phone: str = Field(pattern=r"^[6-9][0-9]{9}$", description="10-digit Indian mobile number")
    items: list[Item] = Field(min_length=1)
    payment_method: Literal["Prepaid", "COD"] = "Prepaid"
    length_cm: float = Field(gt=0)
    breadth_cm: float = Field(gt=0)
    height_cm: float = Field(gt=0)
    weight_kg: float = Field(gt=0)


class AssignAWBInput(BaseModel):
    shipment_id: int = Field(description="shipment_id returned by create_order")
    courier_id: int | None = Field(None, description="courier_company_id from check_serviceability; omit to let Shiprocket choose")


class SchedulePickupInput(BaseModel):
    shipment_ids: list[int] = Field(min_length=1, description="Shipments that already have an AWB")


class TrackShipmentInput(BaseModel):
    awb_code: str = Field(min_length=4, description="AWB number from assign_awb")


class GetOrderInput(BaseModel):
    order_id: int = Field(description="Shiprocket's order_id (from create_order)")


class CancelOrdersInput(BaseModel):
    order_ids: list[int] = Field(min_length=1, description="Shiprocket order_ids to cancel")


def check_serviceability(c: ShiprocketClient, i: CheckServiceabilityInput) -> dict:
    """List couriers that can carry the parcel, cheapest first, with rates and delivery estimates."""
    r = c.request("GET", "/v1/external/courier/serviceability/", params={
        "pickup_postcode": i.pickup_postcode, "delivery_postcode": i.delivery_postcode,
        "weight": i.weight_kg, "cod": int(i.cod)})
    couriers = (r.get("data") or {}).get("available_courier_companies") or []
    if not couriers:
        raise ShiprocketError(Action.FIX_INPUT, f"no courier serves {i.pickup_postcode} -> {i.delivery_postcode} for {i.weight_kg} kg")
    couriers = sorted(couriers, key=lambda x: float(x.get("rate", 0)))
    return {"couriers": [{"courier_id": x["courier_company_id"], "courier_name": x["courier_name"], "rate_inr": float(x["rate"]),
                          "estimated_delivery_days": x.get("estimated_delivery_days")} for x in couriers[:5]],
            "cheapest_courier_id": couriers[0]["courier_company_id"]}


def create_order(c: ShiprocketClient, i: CreateOrderInput) -> dict:
    """Create a shipping order in Shiprocket and return its order_id and shipment_id."""
    body = {
        "order_id": i.order_id, "order_date": i.order_date, "pickup_location": i.pickup_location,
        "billing_customer_name": i.customer_name, "billing_last_name": "", "billing_address": i.address,
        "billing_city": i.city, "billing_pincode": i.pincode, "billing_state": i.state, "billing_country": "India",
        "billing_email": i.email, "billing_phone": i.phone, "shipping_is_billing": True,
        "order_items": [{"name": x.name, "sku": x.sku, "units": x.units, "selling_price": x.selling_price} for x in i.items],
        "payment_method": i.payment_method, "sub_total": sum(x.units * x.selling_price for x in i.items),
        "length": i.length_cm, "breadth": i.breadth_cm, "height": i.height_cm, "weight": i.weight_kg,
    }
    try:
        r = c.request("POST", "/v1/external/orders/create/adhoc", json_body=body)
    except ShiprocketError as e:
        if c.resilient and e.action is Action.FIX_INPUT and "already exists" in e.message.lower():
            return _existing_order(c, i.order_id)  # an earlier attempt went through
        raise
    return {"order_id": r["order_id"], "shipment_id": r["shipment_id"], "status": r.get("status", "NEW")}


def _existing_order(c: ShiprocketClient, channel_order_id: str) -> dict:
    r = c.request("GET", "/v1/external/orders", params={"search": channel_order_id})
    for o in r.get("data") or []:
        if o.get("channel_order_id") == channel_order_id:
            shipments = o.get("shipments") or [{}]
            return {"order_id": o["id"], "shipment_id": shipments[0].get("id"), "status": o.get("status"), "already_existed": True}
    raise ShiprocketError(Action.GIVE_UP, f"Shiprocket says order {channel_order_id} exists but it cannot be found")


def assign_awb(c: ShiprocketClient, i: AssignAWBInput) -> dict:
    """Assign a courier and AWB number to a shipment."""
    body = {"shipment_id": i.shipment_id}
    if i.courier_id is not None:
        body["courier_id"] = i.courier_id
    try:
        r = c.request("POST", "/v1/external/courier/assign/awb", json_body=body)
    except ShiprocketError as e:
        if c.resilient and "already assigned" in e.message.lower():
            s = c.request("GET", f"/v1/external/shipments/{i.shipment_id}")
            d = s.get("data") or {}
            return {"awb_code": d.get("awb"), "courier_name": d.get("courier"), "already_assigned": True}
        raise
    data = ((r.get("response") or {}).get("data")) or {}
    if not data.get("awb_code"):
        raise ShiprocketError(Action.RETRY_LATER, r.get("message") or "no AWB in the response")
    return {"awb_code": data["awb_code"], "courier_name": data.get("courier_name")}


def schedule_pickup(c: ShiprocketClient, i: SchedulePickupInput) -> dict:
    """Ask the courier to pick the shipments up; returns the scheduled date."""
    try:
        r = c.request("POST", "/v1/external/courier/generate/pickup", json_body={"shipment_id": i.shipment_ids})
    except ShiprocketError as e:
        if c.resilient and "already in pickup queue" in e.message.lower():
            return {"pickup_scheduled": True, "already_scheduled": True}
        raise
    resp = r.get("response") or {}
    return {"pickup_scheduled": bool(r.get("pickup_status")), "pickup_date": resp.get("pickup_scheduled_date"),
            "pickup_token": resp.get("pickup_token_number")}


def track_shipment(c: ShiprocketClient, i: TrackShipmentInput) -> dict:
    """Current status and latest scans of a shipment by AWB."""
    r = c.request("GET", f"/v1/external/courier/track/awb/{i.awb_code}")
    t = r.get("tracking_data") or {}
    if t.get("error"):
        raise ShiprocketError(Action.NOT_FOUND, t["error"])
    acts = t.get("shipment_track_activities") or []
    return {"status": (t.get("shipment_track") or [{}])[0].get("current_status"), "etd": t.get("etd"),
            "latest_activity": acts[:3]}


def get_order(c: ShiprocketClient, i: GetOrderInput) -> dict:
    """Details of one Shiprocket order."""
    d = c.request("GET", f"/v1/external/orders/show/{i.order_id}").get("data") or {}
    return {"order_id": d.get("id"), "channel_order_id": d.get("channel_order_id"), "status": d.get("status"),
            "awb_code": (d.get("shipments") or {}).get("awb") if isinstance(d.get("shipments"), dict) else None}


def cancel_orders(c: ShiprocketClient, i: CancelOrdersInput) -> dict:
    """Cancel orders that have not been picked up yet."""
    r = c.request("POST", "/v1/external/orders/cancel", json_body={"ids": i.order_ids})
    return {"cancelled": i.order_ids, "message": r.get("message", "cancelled")}


# name -> (function, input model). The order is the order of the shipping flow.
ACTIONS = {
    "CHECK_SERVICEABILITY": (check_serviceability, CheckServiceabilityInput),
    "CREATE_ORDER": (create_order, CreateOrderInput),
    "ASSIGN_AWB": (assign_awb, AssignAWBInput),
    "SCHEDULE_PICKUP": (schedule_pickup, SchedulePickupInput),
    "TRACK_SHIPMENT": (track_shipment, TrackShipmentInput),
    "GET_ORDER": (get_order, GetOrderInput),
    "CANCEL_ORDERS": (cancel_orders, CancelOrdersInput),
}


def run(c: ShiprocketClient, name: str, arguments: dict) -> dict:
    """Execute an action by name with raw arguments, returning a tool result:
    the action's output, or a structured error the model can act on."""
    fn, model = ACTIONS[name]
    try:
        return {"successful": True, "data": fn(c, model.model_validate(arguments))}
    except ShiprocketError as e:
        return {"successful": False, **e.to_agent()}
    except ValueError as e:  # pydantic validation
        return {"successful": False, **ShiprocketError(Action.FIX_INPUT, str(e)).to_agent()}
    except RuntimeError as e:  # the bare client (no reliability layer): unclassified
        return {"successful": False, "error": str(e)}
