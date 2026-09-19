# Real-stack PAUSED and recovery verification

Prerequisites: seeded development MySQL, Redis and Platform HTTP at localhost:8000.
No physical hardware or infrastructure changes are required.

From the SDK:

```sh
.venv/bin/python scripts/verify_runtime_recovery.py --output-dir /tmp/groundbreaking-recovery-verification
```

The verifier starts temporary real Signaling on port 3002 and a Python SDK process
using the production ControllerRuntime, normal Message handling and backend registry.
`recovery_controller.py` is verification-only: stdin changes SimulatedRobotBackend
state through its existing set_state() seam, or interrupts/reconnects its real
Socket.IO transport. Every runtime observation, control, acknowledgement, snapshot
and neutralization result uses the ordinary protocol. Signaling state is never
injected. No SDK production code changed.

The verifier restores original persistent desired/pause intent and shuts down its
clients/processes in finally. It leaves truthful test session history in development
MySQL. Windows are overridden to 2500 ms Controller grace and 1500 ms Robot recovery.

Verified scenarios:

- OPEN → allocation → first control; B queued; PAUSED → neutralization → rejected
  controls → OPEN; same allocation/session/Robot and fresh control, B stays dormant.
- Unreachable, not-ready and blocking fault interruptions, independently recovered
  by backend observations and positive neutralization; same ownership throughout.
- Controller disconnect → controls blocked/ownership retained → same Controller
  reconnect/new epoch → configure/ack/full snapshot/READY/safety → fresh control.
- Graceful CLOSING → PAUSED(from CLOSING) → CLOSING → leave/neutralization → CLOSED;
  B never starts during drain, and history excludes suspension.
- Final session Controller timeout during paused graceful drain → ENDED with
  controller_recovery_timeout → CLOSED; late reconnect cannot resurrect ownership.
- Robot recovery timeout → ENDED with robot_recovery_timeout; late readiness cannot
  resurrect; every new session requires a distinct allocation UUID.

Evidence: result.json (scenario list, allocation UUIDs, connection epochs, persisted
histories and duration calculations), sdk.log, signaling.log, and temporary srtg.toml.
The final passing run's first session had 5692 ms from start to end, 3044 ms excluded,
and 2648 ms actual play. These are measured results, not hardcoded verifier assertions.

The existing `verify_simulated_runtime.py` also passed separately using real DummyGame,
including retained FIFO reallocation after graceful close, with no Controller restart.
Existing SDK runtime/backend tests: 16 pass. Deterministic overlapping intervals are
covered in Signaling tests, rather than simulated by long live sleeps.
