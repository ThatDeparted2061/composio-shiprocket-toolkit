**An agent completes 50/50 Shiprocket shipping tasks on every seed with this toolkit's reliability layer, against 75% with the same tools on a bare client. That is under production-like trouble: 12% throttling, 6% 5xx, 2% timeouts and 4% of writes losing their response. When the API token also expires mid-run, the bare client falls to 34% and the layer stays at 100%, with no duplicate orders in any run.**

# composio-shiprocket

A [Composio](https://composio.dev) toolkit for [Shiprocket](https://www.shiprocket.in), the shipping aggregator most Indian D2C brands use to book Delhivery, Xpressbees, Blue Dart and others. Composio has a Razorpay toolkit but none for Shiprocket, so an agent that takes an order payment can't ship it.

The toolkit has seven tools covering the core flow: **check serviceability → create order → assign AWB → schedule pickup → track**, plus get and cancel. Underneath sits the layer agents need on real SaaS APIs:

| Problem on a real API | What the layer does |
|---|---|
| The JWT from `POST /auth/login` expires (24 h, or 240 h, depending on whom you ask) | Reads the token's `exp` and refreshes before it lapses (at most a tenth of its life early), plus one fresh login on any 401 |
| 429 throttling, 502/503, timeouts | Exponential backoff with **full jitter**, honouring `Retry-After`, so concurrent agents don't retry in lockstep |
| A create commits, then the response is lost | The retry gets "Order Id … already exists". The layer looks the order up by your `order_id` and returns it: no duplicate, no error. The same applies to AWBs ("already assigned") and pickups ("already in pickup queue") |
| "HTTP 422" means nothing to a model | Every failure carries an **action**: `retry_later`, `reauthenticate`, `fix_input`, `not_found` or `give_up`, with a hint and the field-level message (`pickup_location: Wrong Pickup location entered. Use one of: Primary, Warehouse-BLR`) |
| Bad input wastes a round trip | Pydantic models validate pincodes, phone numbers and weights before any HTTP call. Their field descriptions become the JSON schema the model sees |

## Results

`python -m eval.run_eval` runs 50 scripted agent tasks against a Shiprocket simulator, on three seeds, with and without the layer:

| Seed | Transient faults: bare client | with the layer | + token expiry: bare client | with the layer |
|---:|---:|---:|---:|---:|
| 1 | 36/50 | **50/50** | 4/50 | **50/50** |
| 2 | 39/50 | **50/50** | 24/50 | **50/50** |
| 3 | 37/50 | **50/50** | 23/50 | **50/50** |
| **Mean** | 75% | **100%** | 34% | **100%** |

By task kind (transient faults, seed 1):

| Task | Without | With |
|---|---:|---:|
| ship | 20/28 | 28/28 |
| ship_fix_input | 3/6 | 6/6 |
| cancel | 6/8 | 8/8 |
| unserviceable | 3/4 | 4/4 |
| track_unknown | 4/4 | 4/4 |

Faults injected (transient, seed 1, with the layer): {'requests': 274, '429': 33, '5xx': 11, 'timeouts': 4, 'lost': 9, 'expired_token': 0}
Layer activity: 57 retries across 216 calls.
With token expiry (seed 1): 2 token refreshes, 0 requests hit an expired token.
Why runs without the layer failed (transient, seed 1): {'stopped at awb': 3, 'stopped at pickup': 3, '0 orders exist': 3, 'stopped at track': 2, 'stopped at create': 2, 'did not report the pincode as unserviceable': 1}

The 50 tasks are 28 shipments, 6 shipments naming a pickup address that doesn't exist (the error lists the valid ones), 8 cancellations, 4 shipments to unserviceable pincodes, and 4 lookups of unknown AWBs. The scripted agent behaves the same way in both runs, like a reasonable model would: it follows an error's `action` when there is one, and retries once when the error is unclassified. **Success is judged from the simulator's final state**, not from what the agent says. A shipped order must exist exactly once, with an AWB and a pickup.

Without the layer, most failures are lost write responses. The agent retries the create, gets "already exists", and stops. It also gives up on 429s after one retry. With the token expiring, every call after expiry fails.

## Use it

```python
from composio import Composio
from composio_shiprocket import ShiprocketClient, build_toolkit

composio = Composio()
shiprocket = build_toolkit(composio, ShiprocketClient(email="api-user@yourbrand.in", password="…"))
session = composio.create(user_id="ops", experimental={"custom_toolkits": [shiprocket]})
# the agent now sees SHIPROCKET tools next to Composio's hosted ones (Gmail, Slack, Razorpay…)
```

The toolkit uses the Python SDK's custom toolkit API (`composio.experimental.Toolkit`), so the tools run in-process inside a tool router session. Credentials are a Shiprocket [API user](https://apidocs.shiprocket.in), not your login. For other frameworks, `tool_schemas()` returns plain name/description/JSON-schema definitions, and `run(client, name, args)` executes one.

A failed call returns, for example:

```json
{"successful": false, "action": "fix_input",
 "error": "The given data was invalid. (pickup_location: Wrong Pickup location entered. Use one of: Primary, Warehouse-BLR)",
 "hint": "The request was invalid. Correct the arguments named in the message, then try again.", "status": 422}
```

## Run it

```bash
uv venv && uv pip install -e ".[dev,composio]"
pytest                          # client, actions against the simulator, and the Composio SDK round trip
python -m eval.run_eval         # ~1 min; writes eval/RESULTS.md and eval/results.json
python -m mock.shiprocket_sim   # the simulator on :8765, to point an agent at
```

## Layout

| Path | |
|---|---|
| `src/composio_shiprocket/client.py` | Auth with refresh, jittered retries, the bare mode used as the eval baseline |
| `src/composio_shiprocket/errors.py` | Failure → action classification |
| `src/composio_shiprocket/actions.py` | The seven tools, their input models, retry-safe writes |
| `src/composio_shiprocket/toolkit.py` | Composio registration and framework-neutral schemas |
| `mock/shiprocket_sim.py` | Same paths as `apiv2.shiprocket.in/v1/external`, with faults |
| `eval/run_eval.py` | The 50 tasks, the agent loop, the judge |

## Limits

- The simulator follows Shiprocket's public API docs (paths, payloads, error messages). It has not been run against a live Shiprocket account.
- Composio's own toolkits are hosted by Composio and requested through its [request board](https://request.composio.dev/boards/tool-requests), not merged into the SDK repository. This package is a custom toolkit you can use today.
- Writes are made retry-safe by looking up your own `order_id`. Use a unique one per order.
