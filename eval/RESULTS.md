| Seed | Transient faults: without | with | + token expiry: without | with |
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
