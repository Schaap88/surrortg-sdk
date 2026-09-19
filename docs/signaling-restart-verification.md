# Signaling restart verification

From `surrortg-sdk`, with the development Platform/MySQL and Redis running:

```sh
.venv/bin/python scripts/verify_signaling_restart.py \
  --output-dir /tmp/groundbreaking-signaling-restart-verification
```

The script runs real Signaling and the normal Python SDK `ControllerRuntime` with
a `SimulatedRobotBackend`. A local forwarding proxy forces a transient Player
Session persistence failure while all domain/configuration requests still reach
the real Platform.

It sends `SIGKILL` to three successive Signaling generations while:

- gameplay is ACTIVE and another Player is queued;
- the Controller is inside its recovery window;
- gameplay is PAUSED.

After each restart it proves that queues, reservations, allocations and recovery
ownership did not return; MySQL history becomes terminal with
`signaling_restart`; the Controller performs a fresh configure acknowledgement and
complete snapshot; the Robot stays outside capacity until a newly correlated
neutralization succeeds; and a fresh Player can allocate and control afterward.
The PAUSED scenario also proves that persistent pause intent returns without the
old suspended allocation.

Evidence includes `result.json`, Signaling/SDK logs and the journal directory.
The verifier restores Game intent and deletes only the Player Session rows it
created. Its SDK backend and protocol paths are production code; this script does
not modify production SDK behavior.
