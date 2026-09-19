"""Live seeded vertical slice. Requires running Platform, MySQL and Redis.

Run from the SDK: .venv/bin/python scripts/verify_simulated_runtime.py
Starts temporary real Signaling and DummyGame processes; uses ordinary player
Socket.IO messages. Assertions inspect their normal logs, with no control bypass.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

import aiohttp
import socketio


async def verify(port, output_dir):
    sdk = Path(__file__).resolve().parents[1]
    signaling = sdk.parent / "groundbreaking-signaling"
    output_dir.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession() as session:
        async def get(path):
            async with session.get(f"http://127.0.0.1:8000/api/ge/v2/{path}") as response:
                response.raise_for_status()
                return await response.json()

        config = await get("controllers/dev-controller-001/runtime-config")
        domain = await get(f"games/{config['game_id']}/runtime-domain")
    async def platform_eval(code, *arguments):
        bootstrap = "require 'vendor/autoload.php'; $app = require 'bootstrap/app.php'; $app->make(Illuminate\\Contracts\\Console\\Kernel::class)->bootstrap(); "
        process = await asyncio.create_subprocess_exec("php", "-r", bootstrap + code, *map(str, arguments),
            cwd=sdk.parent / "groundbreaking-platform", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError(stderr.decode())
        return stdout.decode()

    original_desired = domain["desired_availability"]
    async def desired(state):
        await platform_eval("App\\Models\\Game::findOrFail($argv[1])->update(['desired_availability' => $argv[2]]);", config["game_id"], state)

    simulated = next(r for r in config["robots"] if r["implementation_kind"] == "simulated")
    physical = next(r for r in config["robots"] if r["implementation_kind"] == "physical")
    assert simulated["runtime_config"] == {}
    robot_domain = next(r for r in domain["robots"] if r["robot_id"] == simulated["robot_id"])
    assert robot_domain["enabled"] and robot_domain["set_is_eligible"]
    assert not next(r for r in domain["robots"] if r["robot_id"] == physical["robot_id"])["enabled"]
    option = next(q for q in domain["queue_options"] if q["name"] == "Standard")
    assert option["robot_type_id"] == robot_domain["robot_type_id"]
    (output_dir / "domain.json").write_text(json.dumps({"config": config, "domain": domain}, indent=2))
    config_path = output_dir / "srtg.toml"
    config_path.write_text((sdk / "configs/development/srtg.example.toml").read_text().replace(":3000/", f":{port}/"))
    logs = {"signaling": [], "sdk": []}
    processes, readers, clients = [], [], []

    async def capture(process, name):
        with (output_dir / f"{name}.log").open("w") as file:
            while line := await process.stdout.readline():
                text = line.decode(errors="replace")
                logs[name].append(text)
                file.write(text)
                file.flush()

    async def wait_for(predicate, label):
        async with asyncio.timeout(20):
            while not predicate():
                for process in processes:
                    if process.returncode is not None:
                        raise RuntimeError(f"Process exited {process.returncode}; see {output_dir}")
                await asyncio.sleep(.05)
        print(label, flush=True)

    def has(name, text):
        return any(text in line for line in logs[name])

    async def launch(name, *args, cwd, env):
        process = await asyncio.create_subprocess_exec(*args, cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        processes.append(process)
        readers.append(asyncio.create_task(capture(process, name)))

    async def player():
        client = socketio.AsyncClient()
        clients.append(client)
        messages = []

        @client.on("message", namespace="/signaling")
        async def receive(message):
            messages.append(message)

        @client.on("mappingResponse", namespace="/signaling")
        async def receive_mapping(data):
            messages.append({"event": "mappingResponse", "payload": json.loads(data) if isinstance(data, str) else data})

        await client.connect(f"http://127.0.0.1:{port}?clientType=player&gameId={config['game_id']}", namespaces=["/signaling"])
        return client, messages

    async def send(client, event, *, dst="gameEngine", payload=None, seat=0):
        await client.emit("message", {"event": event, "dst": dst, "seat": seat, "payload": payload or {}, "isAdmin": False}, namespace="/signaling")

    def control_count():
        return sum("control applied outputs=" in line for line in logs["sdk"])

    def allocation_count():
        return sum("readiness confirmed allocation=" in line for line in logs["signaling"])

    try:
        await desired("OPEN")
        await launch("signaling", "node", "index.js", cwd=signaling, env={**os.environ,
            "API_BASE_URL": "http://127.0.0.1:8000/api", "REDIS_URL": "redis://127.0.0.1:6379",
            "SOCKETIO_PORT": str(port), "LOG_LEVEL": "debug"})
        await wait_for(lambda: has("signaling", f"Listening on {port}"), "Real Signaling + Redis ready")
        a, am = await player()
        b, bm = await player()
        await launch("sdk", sys.executable, "-m", "games.dummy_game.game", "-c", str(config_path), cwd=sdk,
            env={**os.environ, "PYTHONPATH": str(sdk), "PYTHONUNBUFFERED": "1"})
        await wait_for(lambda: has("signaling", "state=READY") and has("signaling", f"Robot={simulated['robot_id']} reachable=true ready=true eligible=true"),
            "Configuration applied; snapshot accepted; Controller READY; simulated Robot eligible")
        await desired("CLOSED")
        await wait_for(lambda: has("signaling", "RuntimeGame CLOSED"), "Persistent close observed; effective CLOSED")
        await send(a, "joinGame", payload={"queue_option_id": option["id"]})
        await wait_for(lambda: any(m.get("event") == "joinGameResult" and m["payload"].get("reason") == "game_closed" for m in am), "CLOSED explicitly rejects gameplay")
        assert allocation_count() == 0
        await desired("OPEN")
        await wait_for(lambda: sum("RuntimeGame OPEN" in line for line in logs["signaling"]) >= 2, "Persistent open observed")
        control = {"id": "joystick_main", "type": "joystick", "command": {"x": .5, "y": -.75}}
        await send(a, "gameControls", dst="dev-controller-001", payload=control)
        await asyncio.sleep(.3)
        assert control_count() == 0, "Unallocated control delivered"
        await send(a, "joinGame", payload={"queue_option_id": option["id"]})
        aid = a.get_sid("/signaling")
        await wait_for(lambda: allocation_count() == 1 and any(m.get("event") == "enableRouting" for m in am), "Standard reserved, readiness confirmed, allocation committed")
        assert any(m.get("event") == "newPeer" and m["payload"]["id"] == aid and m["payload"]["seat"] == simulated["seat"] for m in am)
        await send(b, "gameControls", dst="dev-controller-001", payload=control, seat=simulated["seat"])
        await asyncio.sleep(.3)
        assert control_count() == 0, "Another player controlled allocated Robot"
        # Supplied destination/seat are overwritten by canonical allocation.
        await send(a, "gameControls", dst="arbitrary-controller", payload=control, seat=99)
        await wait_for(lambda: control_count() == 1, "Player A control reached simulated backend through canonical allocation")
        assert has("sdk", "'x': 0.5, 'y': -0.75")
        await send(b, "joinGame", payload={"queue_option_id": option["id"]})
        await wait_for(lambda: any(m.get("event") == "joinGameResult" and m["payload"].get("status") == "queued" for m in bm), "Player B queued")
        await desired("CLOSED")
        await wait_for(lambda: sum("RuntimeGame CLOSING" in line for line in logs["signaling"]) >= 2, "Graceful close enters CLOSING")
        await a.emit("mapping", namespace="/signaling")
        await wait_for(lambda: any(m.get("event") == "mappingResponse" and any(g["game_id"] == config["game_id"] and g["effective_availability"] == "CLOSING" for g in m["payload"].get("games", [])) for m in am), "Existing mapping boundary exposes effective CLOSING separately from desired CLOSED")
        await send(a, "gameControls", dst="dev-controller-001", payload=control)
        await wait_for(lambda: control_count() == 2, "Committed Player A continues during CLOSING")
        c, cm = await player()
        await send(c, "joinGame", payload={"queue_option_id": option["id"]})
        await wait_for(lambda: any(m.get("event") == "joinGameResult" and m["payload"].get("reason") == "game_closing" for m in cm), "CLOSING rejects new admission")
        assert allocation_count() == 1
        await send(a, "leaveGame")
        await wait_for(lambda: has("sdk", "neutralization succeeded outputs={'joystick_main': {'x': 0, 'y': 0}}") and
            has("signaling", f"neutralization succeeded Robot={simulated['robot_id']} eligible=true"), "Released; correlated neutralization succeeded; outputs neutral; Robot eligible again")
        await send(a, "gameControls", dst="dev-controller-001", payload=control)
        await asyncio.sleep(.3)
        assert control_count() == 2, "Released player still controls Robot"
        await wait_for(lambda: sum("RuntimeGame CLOSED" in line for line in logs["signaling"]) >= 2, "Drain completed synchronously; effective CLOSED")
        await asyncio.sleep(1.2)
        assert allocation_count() == 1, "Dormant queue advanced while CLOSED"
        await desired("OPEN")
        await wait_for(lambda: allocation_count() == 2 and sum(m.get("event") == "enableRouting" for m in bm) == 2, "Second allocation succeeded without restarting Controller")
        await send(b, "gameControls", dst="dev-controller-001", payload=control)
        await wait_for(lambda: control_count() == 3, "Player B control reached same simulated Robot")
        await send(b, "leaveGame")
        await wait_for(lambda: sum("neutralization succeeded Robot=" in line for line in logs["signaling"]) == 2, "Second release neutralized successfully")
        assert sum("configuration applied revision=" in line for line in logs["sdk"]) == 1
        history = None
        for _ in range(100):
            raw = await platform_eval("echo App\\Models\\PlayerSession::where('runtime_player_id', $argv[1])->first()?->toJson() ?? 'null';", aid)
            history = json.loads(raw)
            if history and history["ended_at"]:
                break
            await asyncio.sleep(.1)
        assert history and history["allocated_at"] and history["started_at"] and history["ended_at"]
        assert str(history["game_id"]) == config["game_id"] and str(history["robot_id"]) == simulated["robot_id"]
        assert str(history["queue_option_id"]) == option["id"] and history["end_reason"] == "player_left"
        (output_dir / "player-session.json").write_text(json.dumps(history, indent=2))
        print("Historical session has truthful canonical IDs, allocation/start/end timestamps and reason", flush=True)
        result = {"status": "passed", "game_id": config["game_id"], "queue_option_id": option["id"],
            "robot_id": simulated["robot_id"], "controller_id": config["controller_id"], "seat": simulated["seat"],
            "allocations": 2, "controls": 3, "neutralizations": 2}
        (output_dir / "result.json").write_text(json.dumps(result, indent=2))
        print(f"PASS; evidence: {output_dir}")
    finally:
        for client in clients:
            if client.connected:
                await client.disconnect()
        for process in reversed(processes):
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        await asyncio.gather(*readers)
        await desired(original_desired)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=3001)
    parser.add_argument("--output-dir", type=Path, default=Path(tempfile.mkdtemp(prefix="groundbreaking-simulated-")))
    args = parser.parse_args()
    asyncio.run(verify(args.port, args.output_dir.resolve()))
