# M4 SDK observations (Contracts I and K)

The TCP backend defaults `telemetry_interval` to 1 second. A robot may override
it with a finite positive number in its `runtime_config`. POWER requests still
piggyback on the existing heartbeat; the unchanged default heartbeat is 2 seconds,
so the default physical polling cadence remains 2 seconds. No extra command-path
transactions or firmware changes are introduced.

Simulated backends publish version-1 normalized power observations every second
through `subscribe_telemetry`, using the runtime's existing coalesced
`robot.telemetry_observed` path. Numeric control activity drains the battery by
one percentage point per minute, with a 5 percent floor. Boolean button values
are excluded from load. Current is 200 mA plus 1200 mA times the largest normalized
input magnitude. Driving outward at an arena boundary produces 3800 mA; driving
away from it clears that reading. Voltage, charge and current are valid and fresh;
power and dock are invalid, with dock age unavailable. Stopping a backend cancels
and joins its telemetry task, including a pending asynchronous subscriber.

Runtime configurations may include `stations`, defaulting to an empty list:

```json
{"key": "hopper", "module": "mass_flow", "simulated": true,
 "max_rate_g_per_s": 100}
```

A controller with simulated robots emits `station.observation` every second for
each simulated mass-flow station. A physical-only controller ignores these
stations. The rate eases exponentially toward full scale while a simulated robot
moves, and toward zero otherwise, with a three-second time constant. Total mass
integrates the rate between samples. Rate and total remain below the observation
schema's numeric ceiling. Turning counts as movement; driving into an edge does
not. Station sequences increase independently across task restarts. Rate restarts
at zero on a new configuration or reconnection. Totals are retained
per game and station for the lifetime of the runtime, including reconfiguration
and reconnect; these observations are transient and never settle fuel or award
objectives.

Identical configuration requests keep the existing emitter. Valid replacement
configuration cancels and joins it before installing the replacement; rejected
configuration leaves it running. Disconnect cancels it immediately, and shutdown
joins it. World entries track their backend owner, so an old simulated backend
cannot remove, control or neutralize its replacement's world entry.

Run the SDK suite with pytest's importlib mode (the tests use relative imports):

```sh
PYTHONPATH=. python -m pytest --import-mode=importlib tests
```
