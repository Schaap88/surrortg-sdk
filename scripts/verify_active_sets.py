"""Real MySQL/Redis/Platform/Signaling/SDK active-Set verification; restores fixtures."""
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


async def platform_eval(platform, code, *args):
    bootstrap = "require 'vendor/autoload.php'; $app = require 'bootstrap/app.php'; $app->make(Illuminate\\Contracts\\Console\\Kernel::class)->bootstrap(); "
    process = await asyncio.create_subprocess_exec("php", "-r", bootstrap + code, *map(str, args), cwd=platform,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await process.communicate()
    if process.returncode:
        raise RuntimeError(stderr.decode())
    return stdout.decode()


async def restore_fixture(platform, fixture, allocation_ids):
    await platform_eval(platform,
        "$f = json_decode($argv[1], true); $ids = json_decode($argv[2], true); Illuminate\\Support\\Facades\\DB::transaction(function () use ($f, $ids) { "
        "App\\Models\\PlayerSession::whereIn('id', $ids)->delete(); "
        "App\\Models\\Game::findOrFail($f['game_id'])->update($f['game']); "
        "App\\Models\\Robot::findOrFail($f['robot_id'])->update(['set_id' => $f['original_set_id']]); "
        "foreach ($f['extra_robot_ids'] as $id) App\\Models\\Robot::findOrFail($id)->delete(); "
        "foreach ($f['set_ids'] as $id) App\\Models\\Set::findOrFail($id)->delete(); "
        "App\\Models\\QueueOption::findOrFail($f['option_id'])->update($f['policy']); "
        "if (!$f['had_permission']) App\\Models\\User::findOrFail($f['user_id'])->revokePermissionTo('update Game'); });",
        json.dumps(fixture), json.dumps(allocation_ids))


async def verify(port, output_dir):
    sdk = Path(__file__).resolve().parents[1]
    platform = sdk.parent / "groundbreaking-platform"
    output_dir.mkdir(parents=True, exist_ok=True)
    logs = {"signaling": [], "sdk": []}
    processes, readers, clients, snapshots, scenarios = [], [], [], [], []
    fixture = None
    sequence = 0
    token = uuid.uuid4().hex

    async def php(code, *args):
        return await platform_eval(platform, code, *args)

    async def get(path):
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:8000/api/ge/v2/{path}") as response:
                response.raise_for_status()
                return await response.json()

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

    async def wait(predicate, label):
        async with asyncio.timeout(20):
            while not predicate():
                if any(p.returncode is not None for p in processes):
                    raise RuntimeError(f"Process exited; inspect {output_dir}")
                await asyncio.sleep(.025)
        print(label, flush=True)

    def allocations():
        return re.findall(r"readiness confirmed allocation=([\w-]+) Robot=([\w-]+) player=([\w-]+)", "".join(logs["signaling"]))

    async def send(client, event, payload=None):
        await client.emit("message", {"event": event, "src": client.get_sid("/signaling"),
            "dst": "forged" if event == "gameControls" else "gameEngine", "seat": 99, "payload": payload or {}}, namespace="/signaling")

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

    async def state(capacity, health, outage="AVAILABLE", queued=None, committed=2):
        async with asyncio.timeout(15):
            while True:
                data = await mapping()
                q = next(q for q in data["queue_options"] if q["id"] == option["id"])
                if q["runtime_capable_capacity"] == capacity and data["runtime_health"] == health and q["outage_state"] == outage:
                    if queued is not None and q["queued_players"] != queued:
                        await asyncio.sleep(.025); continue
                    assert data["effective_availability"] == "OPEN", data
                    assert data["committed_gameplay"] == committed, data
                    snapshots.append(data)
                    return data
                await asyncio.sleep(.025)

    async def command(robot_id, ready):
        nonlocal sequence
        sequence += 1
        controller.stdin.write((json.dumps({"event": "state", "sequence": sequence, "robot_id": robot_id,
            "reachable": ready, "ready": ready}) + "\n").encode())
        await controller.stdin.drain()
        await wait(lambda: any(f"command completed {sequence}" in line for line in logs["sdk"]), f"SDK Robot {robot_id} ready={ready}")

    async def join(client, messages, status="queued", reason=None):
        before = len(messages)
        await send(client, "joinGame", {"queue_option_id": option["id"]})
        await wait(lambda: any(m["event"] == "joinGameResult" and m["payload"].get("status") == status and
            (reason is None or m["payload"].get("reason") == reason) for m in messages[before:]), f"Join result {status} {reason or ''}")

    async def fresh(client):
        before = sum("control applied outputs=" in line for line in logs["sdk"])
        await send(client, "gameControls", {"id": "joystick_main", "type": "joystick", "command": {"x": .5, "y": -.75}})
        await wait(lambda: sum("control applied outputs=" in line for line in logs["sdk"]) == before + 1, "Fresh control reached SDK backend")

    async def sessions(ids):
        return json.loads(await php("echo App\\Models\\PlayerSession::whereIn('id', json_decode($argv[1], true))->orderBy('allocated_at')->get()->toJson();", json.dumps(ids)))

    try:
        config = await development_runtime_config(http)
        domain = await get(f"games/{config['game_id']}/runtime-domain")
        option = next(q for q in domain["queue_options"] if q["name"] == "Standard")
        assert len([q for q in domain["queue_options"] if q["enabled"]]) == 1, "Requires the one-option DevelopmentSeeder Game"
        robot = next(r for r in config["robots"] if r["implementation_kind"] == "simulated")
        fixture = json.loads(await php(
            "$r = App\\Models\\Robot::findOrFail($argv[1]); $q = App\\Models\\QueueOption::findOrFail($argv[2]); $g = $q->game; "
            "$u = App\\Models\\User::where('email', Database\\Seeders\\DevelopmentSeeder::ADMIN_EMAIL)->sole(); "
            "$p = Spatie\\Permission\\Models\\Permission::findOrCreate('update Game', 'web'); $had = $u->hasDirectPermission($p); "
            "$result = Illuminate\\Support\\Facades\\DB::transaction(function () use ($r, $q, $g, $u, $p, $had) { "
            "$original = ['robot_id' => $r->id, 'original_set_id' => $r->set_id, 'game_id' => $g->id, 'option_id' => $q->id, 'user_id' => $u->id, 'had_permission' => $had, "
            "'game' => $g->only(['desired_availability', 'pause_requested', 'active_set_id', 'set_switch_id']), 'policy' => $q->only(['expected_capacity', 'healthy_capacity', 'outage_grace_seconds'])]; "
            "$sets = []; foreach (['A', 'B', 'Empty'] as $name) $sets[] = App\\Models\\Set::create(['game_id' => $g->id, 'name' => 'Verification '.$name]); "
            "$extra = []; foreach ([$sets[1]->id, null] as $set) $extra[] = App\\Models\\Robot::create(['controller_id' => $r->controller_id, 'robot_type_id' => $r->robot_type_id, 'set_id' => $set, 'seat' => $r->controller->robots()->max('seat') + 1, 'name' => 'Temporary Set verification Robot', 'enabled' => true, 'implementation_kind' => 'simulated'])->id; "
            "$r->update(['set_id' => $sets[0]->id]); $g->update(['active_set_id' => $sets[0]->id, 'desired_availability' => 'OPEN', 'pause_requested' => false]); "
            "$q->update(['expected_capacity' => 2, 'healthy_capacity' => null, 'outage_grace_seconds' => 30]); $u->givePermissionTo($p); "
            "return $original + ['extra_robot_ids' => $extra, 'set_ids' => array_map(fn ($s) => $s->id, $sets)]; }); echo json_encode($result);",
            robot["robot_id"], option["id"]))
        (output_dir / "fixture.json").write_text(json.dumps(fixture, indent=2))
        config = await development_runtime_config(http)
        ids = [robot["robot_id"], *map(str, fixture["extra_robot_ids"])]
        config_path = output_dir / "srtg.toml"
        config_path.write_text((sdk / "configs/development/srtg.example.toml").read_text().replace(":3000/", f":{port}/"))
        await launch("signaling", "node", "index.js", cwd=sdk.parent / "groundbreaking-signaling", env={**os.environ,
            "API_BASE_URL": "http://127.0.0.1:8000/api", "REDIS_URL": "redis://127.0.0.1:6379", "SOCKETIO_PORT": str(port),
            "LOG_LEVEL": "debug", "PLATFORM_RUNTIME_COMMAND_TOKEN": token})
        await wait(lambda: any(f"Listening on {port}" in line for line in logs["signaling"]), "Real Signaling + Redis ready")
        controller = await launch("sdk", sys.executable, "scripts/recovery_controller.py", "--config", str(config_path), cwd=sdk,
            env={**os.environ, "PYTHONPATH": str(sdk), "PYTHONUNBUFFERED": "1"})
        await wait(lambda: any("state=READY" in line for line in logs["signaling"]), "Real SDK Controller READY")
        a, am, mapping = await player(); u, um, _ = await player(); b, bm, _ = await player(); d, dm, _ = await player()

        async def switch(set_id, command_id=None):
            command_id = command_id or str(uuid.uuid4())
            ack = json.loads(await php(
                "$u = App\\Models\\User::where('email', Database\\Seeders\\DevelopmentSeeder::ADMIN_EMAIL)->sole(); auth()->login($u); "
                "config(['services.signaling_runtime.url' => $argv[4], 'services.signaling_runtime.token' => $argv[5]]); "
                "echo json_encode(app(App\\Services\\GameSetSwitch::class)->execute(App\\Models\\Game::findOrFail($argv[1]), $argv[2], $argv[3]));",
                config["game_id"], str(set_id), command_id, f"http://127.0.0.1:{port}", token))
            assert ack["active_set_id"] == str(set_id), ack
            return ack

        await state(2, "HEALTHY", queued=0, committed=0)
        initial = await get(f"games/{config['game_id']}/runtime-domain")
        eligible = {r["robot_id"]: r["set_is_eligible"] for r in initial["robots"]}
        assert eligible[ids[0]] and not eligible[ids[1]] and eligible[ids[2]], eligible
        await join(a, am, "confirming"); await wait(lambda: len(allocations()) == 1, "Player A committed to Robot A")
        assert allocations()[0][1] == ids[0], allocations()
        await fresh(a)
        await join(u, um, "confirming"); await wait(lambda: len(allocations()) == 2, "Ungrouped Robot occupied")
        assert allocations()[1][1] == ids[2], allocations()
        await join(b, bm); await state(2, "HEALTHY", queued=1)
        switch_id = str(uuid.uuid4()); ack = await switch(fixture["set_ids"][1], switch_id)
        await wait(lambda: len(allocations()) == 3, "FIFO waiting Player B committed to newly eligible Robot B")
        assert allocations()[2][1] == ids[1] and allocations()[2][2] == b.get_sid("/signaling"), allocations()
        await state(2, "HEALTHY", queued=0, committed=3)
        await fresh(a); await fresh(b)
        same = await switch(fixture["set_ids"][1], switch_id); assert same == ack
        history = await sessions([entry[0] for entry in allocations()])
        assert len(history) == 3 and all(row["ended_at"] is None for row in history), history
        scenarios.append("A + ungrouped eligible; occupied A persists with SDK controls after switch B; FIFO commits B; A excluded from capacity; duplicate switch idempotent")
        await send(a, "leaveGame")
        await state(2, "HEALTHY", queued=0, committed=2)
        await join(d, dm); await state(2, "HEALTHY", queued=1, committed=2)
        assert len(allocations()) == 3, allocations()  # A is free but inactive.
        await switch(fixture["set_ids"][0]); await wait(lambda: len(allocations()) == 4, "A allocates again only after reactivation")
        assert allocations()[3][1] == ids[0], allocations()
        await fresh(d); await fresh(b)
        await state(2, "HEALTHY", queued=0, committed=3)
        scenarios.append("Released inactive A cannot take waiting player; reactivating A advances that same FIFO entry; committed B remains playable")
        await command(ids[2], False); await state(1, "DEGRADED", queued=0, committed=3)
        await join(a, am); await switch(fixture["set_ids"][2])
        grace = await state(0, "UNAVAILABLE", "OUTAGE_GRACE", queued=1, committed=3)
        q = next(q for q in grace["queue_options"] if q["id"] == option["id"])
        assert q["outage_deadline"] is not None, q
        await fresh(d); await fresh(b)
        await command(ids[2], True); await state(1, "DEGRADED", queued=1, committed=3)
        assert len(allocations()) == 4
        scenarios.append("Empty Set + unavailable ungrouped Robot gives UNAVAILABLE with normal 30s outage grace; committed A/B controls survive; returning ungrouped capacity recovers without fallback switching")
        result = {"status": "scenarios_passed", "scenarios": scenarios, "snapshots": snapshots, "allocations": allocations(), "switch_ack": ack, "grace": grace}
        (output_dir / "result.json").write_text(json.dumps(result, indent=2))
    finally:
        for client in clients:
            if client.connected:
                await client.disconnect()
        # Allow normal terminal history delivery before stopping the service.
        if fixture and allocations():
            try:
                async with asyncio.timeout(10):
                    while True:
                        history = await sessions([entry[0] for entry in allocations()])
                        if len(history) == len(allocations()) and all(s["ended_at"] for s in history):
                            break
                        await asyncio.sleep(.05)
            except (TimeoutError, RuntimeError) as error:
                print(f"Terminal history wait failed; continuing fixture cleanup: {error}", flush=True)
        for process in reversed(processes):
            if process.returncode is None:
                process.terminate()
                try: await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError: process.kill(); await process.wait()
        await asyncio.gather(*readers)
        if fixture:
            await restore_fixture(platform, fixture, [entry[0] for entry in allocations()])
    result.update(status="passed", fixture_restored=True)
    (output_dir / "result.json").write_text(json.dumps(result, indent=2))
    print(f"PASS; evidence: {output_dir}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=3005)
    parser.add_argument("--output-dir", type=Path, default=Path(tempfile.mkdtemp(prefix="groundbreaking-active-sets-")))
    parser.add_argument("--restore-from", type=Path, help="Restore this verifier's saved fixture after an interrupted run (stop its processes first)")
    args = parser.parse_args()
    if args.restore_from:
        fixture = json.loads((args.restore_from / "fixture.json").read_text())
        log = (args.restore_from / "signaling.log").read_text()
        ids = re.findall(r"readiness confirmed allocation=([\w-]+)", log)
        asyncio.run(restore_fixture(Path(__file__).resolve().parents[2] / "groundbreaking-platform", fixture, ids))
        print("Verifier fixture restored")
    else:
        asyncio.run(verify(args.port, args.output_dir.resolve()))
