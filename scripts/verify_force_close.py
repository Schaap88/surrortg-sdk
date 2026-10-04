"""Real MySQL/Redis/Platform/Signaling/SDK Force Close verification.

Requires the seeded local stack. Starts temporary Signaling and a real SDK
ControllerRuntime process with the SimulatedRobotBackend. Stdin changes backend
observations or interrupts transport; all Signaling traffic uses normal events.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

import aiohttp
import socketio
from controller_admission import development_runtime_config


async def verify(port, output_dir):
    sdk = Path(__file__).resolve().parents[1]
    platform = sdk.parent / "groundbreaking-platform"
    output_dir.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession() as http:
        async def get(path):
            async with http.get(f"http://127.0.0.1:8000/api/ge/v2/{path}") as response:
                response.raise_for_status()
                return await response.json()
        config = await development_runtime_config(http)
        domain = await get(f"games/{config['game_id']}/runtime-domain")
    robot = next(r for r in config["robots"] if r["implementation_kind"] == "simulated")
    option = next(q for q in domain["queue_options"] if q["name"] == "Standard")
    original = (domain["desired_availability"], domain["pause_requested"])
    token = uuid.uuid4().hex
    logs = {"signaling": [], "sdk": []}
    processes, readers, clients, scenarios = [], [], [], []
    command_sequence = 0

    async def platform_eval(code, *args):
        bootstrap = "require 'vendor/autoload.php'; $app = require 'bootstrap/app.php'; $app->make(Illuminate\\Contracts\\Console\\Kernel::class)->bootstrap(); "
        process = await asyncio.create_subprocess_exec("php", "-r", bootstrap + code, *map(str, args), cwd=platform,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError(stderr.decode())
        return stdout.decode()

    async def intent(desired, paused):
        await platform_eval("App\\Models\\Game::findOrFail($argv[1])->update(['desired_availability' => $argv[2], 'pause_requested' => $argv[3] === '1']);",
            config["game_id"], desired, "1" if paused else "0")

    async def capture(process, name):
        with (output_dir / f"{name}.log").open("w") as file:
            while line := await process.stdout.readline():
                text = line.decode(errors="replace")
                logs[name].append(text); file.write(text); file.flush()

    async def launch(name, *args, cwd, env):
        process = await asyncio.create_subprocess_exec(*args, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        processes.append(process); readers.append(asyncio.create_task(capture(process, name)))
        return process

    def count(name, text):
        return sum(text in line for line in logs[name])

    async def wait(predicate, label):
        async with asyncio.timeout(20):
            while not predicate():
                if any(p.returncode is not None for p in processes):
                    raise RuntimeError(f"Process exited; inspect {output_dir}")
                await asyncio.sleep(.025)
        print(label, flush=True)

    async def player():
        client, messages, mappings = socketio.AsyncClient(), [], asyncio.Queue()
        clients.append(client)
        @client.on("message", namespace="/signaling")
        async def receive(message):
            messages.append(message)
        @client.on("mappingResponse", namespace="/signaling")
        async def receive_mapping(data):
            await mappings.put(json.loads(data) if isinstance(data, str) else data)
        await client.connect(f"http://127.0.0.1:{port}?clientType=player&gameId={config['game_id']}", namespaces=["/signaling"])
        async def mapping():
            await client.emit("mapping", namespace="/signaling")
            data = await asyncio.wait_for(mappings.get(), 3)
            return next(g for g in data["games"] if g["game_id"] == config["game_id"])
        return client, messages, mapping

    async def effective(mapping, state, committed=None, paused_from=None):
        async with asyncio.timeout(10):
            while True:
                data = await mapping()
                if data["effective_availability"] == state:
                    if committed is not None: assert data["committed_gameplay"] == committed, data
                    if paused_from is not None: assert data["paused_from"] == paused_from, data
                    return data
                await asyncio.sleep(.05)

    async def send(client, event, payload=None):
        await client.emit("message", {"event": event, "src": client.get_sid("/signaling"), "dst": "gameEngine" if event != "gameControls" else "forged",
            "seat": 99, "payload": payload or {}}, namespace="/signaling")

    async def command(event, **values):
        nonlocal command_sequence
        command_sequence += 1
        controller.stdin.write((json.dumps({"event": event, "sequence": command_sequence, **values}) + "\n").encode())
        await controller.stdin.drain()
        await wait(lambda: count("sdk", f"command completed {command_sequence}") > 0, f"SDK command {event} completed")

    controls = lambda: count("sdk", "control applied outputs=")
    allocations = lambda: re.findall(r"readiness confirmed allocation=([\w-]+)", "".join(logs["signaling"]))
    control = {"id": "joystick_main", "type": "joystick", "command": {"x": .5, "y": -.75}}
    async def fresh(client):
        before = controls(); await send(client, "gameControls", control)
        await wait(lambda: controls() == before + 1, "Fresh control reached normal SDK backend")
    async def blocked(client):
        before = controls(); await send(client, "gameControls", control); await asyncio.sleep(.2)
        assert controls() == before, "Suspended input reached backend"
    async def history(allocation_id):
        async with asyncio.timeout(15):
            while True:
                raw = await platform_eval("$s = App\\Models\\PlayerSession::find($argv[1]); echo $s ? json_encode(['session' => $s->toArray(), 'play_ms' => $s->play_duration_ms, 'allocated_ms' => $s->allocated_duration_ms]) : 'null';", allocation_id)
                data = json.loads(raw)
                if data and data["session"]["ended_at"]:
                    return data
                await asyncio.sleep(.05)

    config_path = output_dir / "srtg.toml"
    config_path.write_text((sdk / "configs/development/srtg.example.toml").read_text().replace(":3000/", f":{port}/"))
    histories = []
    try:
        await intent("OPEN", False)
        await launch("signaling", "node", "index.js", cwd=sdk.parent / "groundbreaking-signaling", env={**os.environ,
            "API_BASE_URL": "http://127.0.0.1:8000/api", "REDIS_URL": "redis://127.0.0.1:6379", "SOCKETIO_PORT": str(port), "LOG_LEVEL": "debug",
            "CONTROLLER_RECONNECT_GRACE_MS": "15000", "ROBOT_RECOVERY_WINDOW_MS": "10000", "PLATFORM_RUNTIME_COMMAND_TOKEN": token})
        await wait(lambda: count("signaling", f"Listening on {port}") > 0, "Real Signaling + Redis ready")
        controller = await launch("sdk", sys.executable, "scripts/recovery_controller.py", "--config", str(config_path), cwd=sdk,
            env={**os.environ, "PYTHONPATH": str(sdk), "PYTHONUNBUFFERED": "1"})
        await wait(lambda: count("signaling", "state=READY") > 0, "Real SDK admission/acknowledgement/snapshot READY")
        a, am, mapping = await player(); b, bm, _ = await player()
        await send(a, "joinGame", {"queue_option_id": option["id"]})
        await wait(lambda: len(allocations()) == 1, "Player A allocation committed")
        first = allocations()[0]; await fresh(a)
        await send(b, "joinGame", {"queue_option_id": option["id"]})
        await wait(lambda: any(m["event"] == "joinGameResult" and m["payload"].get("status") == "queued" for m in bm), "Player B queued")
        async def force_close():
            operation_id = str(uuid.uuid4())
            raw = await platform_eval("$u = App\\Models\\User::where('email', Database\\Seeders\\DevelopmentSeeder::ADMIN_EMAIL)->sole(); $u->givePermissionTo(Spatie\\Permission\\Models\\Permission::findOrCreate('update Game', 'web')); auth()->login($u); config(['services.signaling_runtime.url' => $argv[3], 'services.signaling_runtime.token' => $argv[4]]); echo json_encode(app(App\\Services\\GameForceClose::class)->execute(App\\Models\\Game::findOrFail($argv[1]), $argv[2]));",
                config["game_id"], operation_id, f"http://127.0.0.1:{port}", token)
            result = json.loads(raw)
            assert result["effective_state"] == "CLOSED", result
            assert result["allocations_released"] == 1, result
            await effective(mapping, "CLOSED", 0)
            await blocked(a)
            async with aiohttp.ClientSession() as http:
                async with http.get(f"http://127.0.0.1:8000/api/ge/v2/games/{config['game_id']}/runtime-domain") as response:
                    actual = await response.json()
                    assert actual["desired_availability"] == "CLOSED" and actual["pause_requested"] is False
                async with http.post(f"http://127.0.0.1:{port}/runtime/games/{config['game_id']}/force-close", headers={"Authorization": f"Bearer {token}"}, json={"force_close_id": operation_id}) as response:
                    assert response.status == 200
                    assert await response.json() == result
            return result

        before_neutral = count("sdk", "neutralization succeeded")
        closed = await force_close()
        assert closed["queue_entries_removed"] == 1
        await wait(lambda: count("sdk", "neutralization succeeded") > before_neutral, "Force Close neutralization succeeded")
        histories.append(await history(first))
        assert histories[-1]["session"]["end_reason"] == "force_closed"
        assert any(m["payload"].get("reason") == "force_closed" for m in bm if m["event"] == "joinGameResult")
        scenarios.append("Authorized Platform command: OPEN teardown + queued player notification + neutralization + terminal MySQL history + duplicate command")
        await intent("OPEN", False); await effective(mapping, "OPEN", 0)
        await asyncio.sleep(.3); assert allocations() == [first], "Old queue returned after reopen"
        await send(a, "joinGame", {"queue_option_id": option["id"]})
        await wait(lambda: len(allocations()) == 2, "Fresh player admission after reopen")
        second = allocations()[1]; await fresh(a)
        await intent("OPEN", True); await effective(mapping, "PAUSED", 1, "OPEN")
        await asyncio.sleep(.2)
        await force_close(); histories.append(await history(second))
        assert histories[-1]["session"]["end_reason"] == "force_closed"
        assert histories[-1]["session"]["paused_duration_ms"] > 0
        assert histories[-1]["play_ms"] < histories[-1]["allocated_ms"]
        scenarios.append("PAUSED Force Close without Resume; persisted suspension excluded from play time")
        await intent("OPEN", False); await effective(mapping, "OPEN", 0)
        await send(a, "joinGame", {"queue_option_id": option["id"]})
        await wait(lambda: len(allocations()) == 3, "Recovery session allocated")
        third = allocations()[2]; await fresh(a)
        await command("disconnect")
        await wait(lambda: count("signaling", "controller_recovery started") > 0, "Controller recovery started")
        recovered_close = await force_close()
        assert recovered_close["recoveries_cancelled"] == 1
        histories.append(await history(third))
        assert histories[-1]["session"]["end_reason"] == "force_closed"
        await command("reconnect"); await asyncio.sleep(.3); await blocked(a)
        assert allocations() == [first, second, third]
        scenarios.append("Controller recovery cancelled immediately; reconnect cannot resurrect old session/allocation")
        epochs = re.findall(r'connection_epoch:\s*"([\w-]+)"', "".join(logs["signaling"]))
        result = {"status": "passed", "scenarios": scenarios, "allocation_ids": allocations(), "connection_epochs": sorted(set(epochs)), "histories": histories}
        (output_dir / "result.json").write_text(json.dumps(result, indent=2))
        print(f"PASS; evidence: {output_dir}", flush=True)
    finally:
        for client in clients:
            if client.connected: await client.disconnect()
        for process in reversed(processes):
            if process.returncode is None:
                process.terminate()
                try: await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError: process.kill(); await process.wait()
        await asyncio.gather(*readers)
        await intent(*original)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=3003)
    parser.add_argument("--output-dir", type=Path, default=Path(tempfile.mkdtemp(prefix="groundbreaking-force-close-")))
    args = parser.parse_args()
    asyncio.run(verify(args.port, args.output_dir.resolve()))
