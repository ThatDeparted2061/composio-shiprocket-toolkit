"""Register the actions as a Composio toolkit.

Uses the Python SDK's custom toolkit API (``composio.experimental.Toolkit``):
the tools run in-process next to Composio's hosted tools in a tool router
session, so an agent can mix Shiprocket with Gmail, Slack and the rest.

    from composio import Composio
    from composio_shiprocket import ShiprocketClient, build_toolkit

    composio = Composio()
    shiprocket = build_toolkit(composio, ShiprocketClient(email, password))
    session = composio.create(user_id="ops", experimental={"custom_toolkits": [shiprocket]})
"""

from __future__ import annotations

from typing import Any

from . import actions as a
from .client import ShiprocketClient


def build_toolkit(composio: Any, client: ShiprocketClient):
    """Return a Composio custom toolkit (slug SHIPROCKET) with all seven tools.

    Tool results are dicts: the action's output on success, or
    ``{"successful": false, "error", "action", "hint"}`` so the model knows
    whether to retry, fix its input, or stop.
    """
    tk = composio.experimental.Toolkit(
        slug="SHIPROCKET",
        name="Shiprocket",
        description="Ship orders across Indian couriers: rates, orders, AWBs, pickups, tracking and cancellations.",
    )

    @tk.tool(slug="CHECK_SERVICEABILITY", description=a.check_serviceability.__doc__)
    def check_serviceability(input: a.CheckServiceabilityInput, ctx):
        return a.run(client, "CHECK_SERVICEABILITY", input.model_dump())

    @tk.tool(slug="CREATE_ORDER", description=a.create_order.__doc__)
    def create_order(input: a.CreateOrderInput, ctx):
        return a.run(client, "CREATE_ORDER", input.model_dump())

    @tk.tool(slug="ASSIGN_AWB", description=a.assign_awb.__doc__)
    def assign_awb(input: a.AssignAWBInput, ctx):
        return a.run(client, "ASSIGN_AWB", input.model_dump())

    @tk.tool(slug="SCHEDULE_PICKUP", description=a.schedule_pickup.__doc__)
    def schedule_pickup(input: a.SchedulePickupInput, ctx):
        return a.run(client, "SCHEDULE_PICKUP", input.model_dump())

    @tk.tool(slug="TRACK_SHIPMENT", description=a.track_shipment.__doc__)
    def track_shipment(input: a.TrackShipmentInput, ctx):
        return a.run(client, "TRACK_SHIPMENT", input.model_dump())

    @tk.tool(slug="GET_ORDER", description=a.get_order.__doc__)
    def get_order(input: a.GetOrderInput, ctx):
        return a.run(client, "GET_ORDER", input.model_dump())

    @tk.tool(slug="CANCEL_ORDERS", description=a.cancel_orders.__doc__)
    def cancel_orders(input: a.CancelOrdersInput, ctx):
        return a.run(client, "CANCEL_ORDERS", input.model_dump())

    return tk


def tool_schemas() -> list[dict]:
    """Framework-neutral tool definitions (name, description, JSON schema),
    e.g. for an OpenAI- or Anthropic-style tool list."""
    return [{"name": f"SHIPROCKET_{name}", "description": (fn.__doc__ or "").strip(), "input_schema": model.model_json_schema()}
            for name, (fn, model) in a.ACTIONS.items()]
