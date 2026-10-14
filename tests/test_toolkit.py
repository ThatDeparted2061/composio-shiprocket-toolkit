import pytest

composio = pytest.importorskip("composio")
from composio.core.models.custom_tool import serialize_custom_toolkits  # noqa: E402

from composio_shiprocket import ShiprocketClient, build_toolkit  # noqa: E402
from mock.shiprocket_sim import Faults, serve  # noqa: E402
from tests.test_actions import ORDER  # noqa: E402


def test_registers_with_the_composio_sdk_and_runs_in_process():
    srv, _ = serve(0, Faults(lost_response=1.0))  # every write's response is lost
    try:
        client = ShiprocketClient("api-user@example.com", "s3cret", f"http://127.0.0.1:{srv.server_port}", base_delay=0.001)
        tk = build_toolkit(composio.Composio(api_key="offline"), client)
        [wire] = serialize_custom_toolkits([tk])  # what a tool router session sends to Composio
        assert wire["slug"] == "SHIPROCKET" and len(wire["tools"]) == 7
        assert all(t["description"] and t["input_schema"]["properties"] for t in wire["tools"])
        tools = {t.slug: t for t in tk.tools}
        create = next(t for slug, t in tools.items() if slug.endswith("CREATE_ORDER"))
        out = create.execute(create.input_params.model_validate(ORDER), None)
        assert out["successful"] and out["data"]["already_existed"]
    finally:
        srv.shutdown()
