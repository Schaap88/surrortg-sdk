# Simulated Robot runtime slice

## Files changed

SDK:

- `surrortg/runtime/simulated_backend.py`
- `surrortg/runtime/backend.py`
- `surrortg/runtime/backend_registry.py`
- `surrortg/runtime/controller_runtime.py`
- `surrortg/runtime/__init__.py`
- `tests/surrortg/runtime/test_simulated_backend.py`
- `tests/surrortg/runtime/test_controller_runtime.py`
- `tests/surrortg/runtime/test_development_bootstrap.py`
- `scripts/verify_simulated_runtime.py`
- `docs/simulated-runtime-verification.md`

Signaling:

- `runtime/runtimeCoordinator.js`
- `events/socketHandlers.js`
- `tests/simulated-runtime.test.js`

Platform: no files changed. The SDK's existing socket handler and its test edits
were preserved without modification.

## Implementation

The Platform already supplies `implementation_kind`, including an empty backend
configuration serialized as `{}`. No Platform change was needed. Previously the
SDK selected a backend solely by address: the seeded simulated Robot received
the inert `Backend`, whose status remains unreachable/not ready.

`BackendRegistry` now selects `SimulatedRobotBackend` for `simulated`, regardless
of address. `physical` retains TCP with a configured address and the unavailable
generic backend without one. Unknown kinds are rejected. Physical neutralization
remains `unsupported`. TCP imports are deferred until connection so constructing
a backend does not require optional hardware packages.

The simulated backend owns `started`, status (`reachable`, `ready`, `faults`),
`neutralized`, `last_control`, and outputs keyed by input ID. `set_state` provides
deterministic loss/fault/recovery observations; loss or faults also zero outputs.
Startup establishes reachable/ready, and stop clears status and zeroes outputs.

Controls use the existing `{id, type, command}` envelope. This minimal backend
supports finite numeric and boolean command values, including nested maps;
unsupported shapes are rejected. Neutral means every number is zero and every
boolean is false, preserving command shape. For the development joystick it is
`{'joystick_main': {'x': 0, 'y': 0}}`. Neutralization requires a started/reachable
backend, zeroes outputs, clears `last_control`, verifies the result, and only then
returns `succeeded`. A fresh authorized routed command can activate outputs again;
`neutralized` describes output state rather than an allocation lock.

ControllerRuntime now consumes existing peer/seat enable/disable messages and
delivers `gameControls` to the common `Backend.apply_control` method after checking
the current peer, seat, destination and routing state. Legacy game input routing
still runs for physical devices. Release removes ownership through existing
`peerLeft`/`disableRouting` messages. No simulation event was introduced.

Backend subscriptions now feed normal Robot observation events. Initial status
emits configuration acknowledgement, reachability/readiness changes, then the
complete snapshot. Controller READY still requires exact acknowledgement plus
accepted snapshot. Replaced backends and disconnected epochs cannot emit current
positive observations. Readiness confirmation queries actual backend status;
missing readiness, reachability or faults fail it. Epoch/revision checks and the
correlated neutralization result cache remain authoritative, including duplicates.

## Contract issues and defects

CONTRACT ISSUE: the existing SDK seam lacked a control method and ControllerRuntime
never subscribed to Backend observations. Evidence: `Backend.subscribe` only
stored a callback, while `ControllerRuntime.handle_message` handled configuration,
snapshots, readiness and neutralization but no controls. The smallest correction
was the common optional `apply_control` method and normal status subscriptions.
Work proceeded without altering Signaling contracts or transport.

Signaling also did not process queued players after accepting a startup snapshot.
That accepted snapshot now invokes the existing queue processor, with regression
coverage for a player queued before READY. Eligibility and allocation rules have
no simulated special case. Allocation retains all canonical IDs, player identity,
seat and reservation ID; failed active allocations do not migrate.

## Automated verification

From `surrortg-sdk`:

```bash
.venv/bin/python -m unittest \
  tests.surrortg.runtime.test_controller_runtime \
  tests.surrortg.runtime.test_development_bootstrap \
  tests.surrortg.runtime.test_simulated_backend \
  tests.surrortg.network.test_socket_handler
```

Result: 21 tests passed, including selection, lifecycle, observation subscriptions,
fault/recovery, owner routing, current readiness, stale epoch/revision, verified
neutralization, duplicate safety after reuse, and conservative physical results.
The existing socket handler edits were already present and were not changed here.

From `groundbreaking-signaling`: `npm test` passed all five test files, including
the simulated fixture scenario. Existing allocation, transport, reconciliation,
and neutralization tests remain in the suite.

From `groundbreaking-platform`: `php artisan test --filter=RuntimeBootstrapContractTest`
passed (one test, six assertions) against isolated `filament_admin_testing` on real
MySQL. This preserves empty backend config `{}` and canonical digest coverage.

## Real stack verification

Performed on 2026-09-16 using existing healthy Docker MySQL 8.4.6 and Redis
7.4.11, real Platform HTTP at port 8000, a temporary real Signaling service at
port 3001 with the Redis adapter, Python 3.13.15, the real DummyGame SDK process,
and two real Python Socket.IO player clients. No databases were reseeded.

From `surrortg-sdk`:

```bash
.venv/bin/python scripts/verify_simulated_runtime.py \
  --output-dir /tmp/groundbreaking-simulated-verification
```

The script starts `node index.js` with `API_BASE_URL=http://127.0.0.1:8000/api`,
`REDIS_URL=redis://127.0.0.1:6379`, `SOCKETIO_PORT=3001`, and `LOG_LEVEL=debug`.
It runs the existing SDK command with `PYTHONPATH` set to the SDK directory and
the development example TOML copied into the evidence directory with only its
Socket.IO port changed to 3001. It terminates only its own temporary processes.

The final run passed with Game `1`, Standard Queue Option `1`, Robot `1`,
Controller `dev-controller-001`, seat `0`: two allocations, two applied controls,
two successful correlated neutralizations, and only one configuration application.
Robot `2` (physical, disabled) remained ineligible. The scenario also rejected
controls before allocation, from the other player, and after release. A legitimate
owner's arbitrary destination/seat were replaced with the canonical allocation.

Representative recorded milestones:

```text
Controller dev-controller-001 admitted and configuring
controller.configuration_applied ... result=accepted
snapshot accepted ... state=READY
Robot=1 reachable=true ready=true eligible=true
Robot=2 reachable=false ready=false eligible=false
reservation ... Robot=1 readiness requested
readiness confirmed allocation ... Robot=1 player=Player A
Robot 1 control applied outputs={'joystick_main': {'x': 0.5, 'y': -0.75}}
Released allocation for Player A
Robot 1 neutralization succeeded outputs={'joystick_main': {'x': 0, 'y': 0}}
neutralization succeeded Robot=1 eligible=true
readiness confirmed allocation ... Robot=1 player=Player B
Robot 1 control applied ...
Released allocation for Player B
neutralization succeeded Robot=1 eligible=true
```

Evidence files: `domain.json`, `result.json`, `sdk.log`, `signaling.log`, and the
temporary `srtg.toml` under the output directory. Assertions use normal process
logs and ordinary player messages; there is no test-only runtime bypass.

Limitation discovered after successful gameplay verification: existing SDK
SIGTERM transport shutdown prints `asyncio.exceptions.CancelledError` and an
unclosed aiohttp session. This transport cleanup issue was not modified. The
simulated backend's own stop lifecycle passes focused tests.

No Player Sessions, session timing, lifecycle states, Set rotation, recovery
policy, migration, transport redesign, infrastructure changes, graphical/physics
simulation, media, firmware or distributed ownership were added.
