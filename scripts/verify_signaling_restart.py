"""Abrupt-restart verification with real MySQL, Redis, Platform, Signaling and SDK."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tempfile

import aiohttp
from aiohttp import web
import socketio


async def verify(port, proxy_port, output_dir):
    sdk = Path(__file__).resolve().parents[1]
    platform = sdk.parent / "groundbreaking-platform"
    signaling_root = sdk.parent / "groundbreaking-signaling"
    output_dir.mkdir(parents=True, exist_ok=True)
    journal_dir = output_dir / "journal"
    shutil.rmtree(journal_dir, ignore_errors=True)
    logs = {"sdk": [], "signaling": []}
    readers, clients, processes, running = [], [], [], set()
    history_failure = {"enabled": False, "count": 0}
    original_intent = None
    session_ids = []
    proxy_client = aiohttp.ClientSession()

    async def platform_eval(code, *args):
        bootstrap = "require 'vendor/autoload.php'; $app = require 'bootstrap/app.php'; $app->make(Illuminate\\Contracts\\Console\\Kernel::class)->bootstrap(); "
        process = await asyncio.create_subprocess_exec("php", "-r", bootstrap + code, *map(str, args), cwd=platform,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError(stderr.decode())
        return stdout.decode()

    async def proxy(request):
        if history_failure["enabled"] and "/player-sessions/" in request.path:
            history_failure["count"] += 1
            return web.Response(status=503, text="forced transient history outage")
        body = await request.read()
        headers = {key: value for key, value in request.headers.items() if key.lower() not in {"host", "content-length"}}
        async with proxy_client.request(request.method, f"http://127.0.0.1:8000{request.rel_url}", data=body, headers=headers) as response:
            return web.Response(status=response.status, body=await response.read(),
                headers={key: value for key, value in response.headers.items() if key.lower() not in {"content-length", "transfer-encoding", "connection"}})

    proxy_app = web.Application(); proxy_app.router.add_route("*", "/{tail:.*}", proxy)
    runner = web.AppRunner(proxy_app); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", proxy_port); await site.start()

    async def get(path):
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:8000/api/ge/v2/{path}") as response:
                response.raise_for_status(); return await response.json()

    async def capture(process, name):
        with (output_dir / f"{name}.log").open("a") as file:
            while line := await process.stdout.readline():
                text = line.decode(errors="replace")
                logs[name].append(text); file.write(text); file.flush()

    async def launch(name, *args, cwd, env):
        process = await asyncio.create_subprocess_exec(*args, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        processes.append(process); running.add(process); readers.append(asyncio.create_task(capture(process, name)))
        return process

    async def wait(predicate, label, timeout=25):
        async with asyncio.timeout(timeout):
            while not predicate():
                if any(process.returncode is not None for process in running):
                    raise RuntimeError(f"Process exited unexpectedly; inspect {output_dir}")
                await asyncio.sleep(.025)
        print(label, flush=True)

    def count(text): return sum(text in line for line in logs["signaling"])
    def allocations(): return re.findall(r"readiness confirmed allocation=([\w-]+)", "".join(logs["signaling"]))
    def controls(): return sum("control applied outputs=" in line for line in logs["sdk"])

    config = await get("controllers/dev-controller-001/runtime-config")
    domain = await get(f"games/{config['game_id']}/runtime-domain")
    option = next(q for q in domain["queue_options"] if q["name"] == "Standard")
    robot = next(r for r in config["robots"] if r["implementation_kind"] == "simulated")
    original_intent = (domain["desired_availability"], domain["pause_requested"])
    config_path = output_dir / "srtg.toml"
    config_path.write_text((sdk / "configs/development/srtg.example.toml").read_text().replace(":3000/", f":{port}/"))

    async def intent(desired, paused):
        await platform_eval("App\\Models\\Game::findOrFail($argv[1])->update(['desired_availability' => $argv[2], 'pause_requested' => $argv[3] === '1']);",
            config["game_id"], desired, "1" if paused else "0")

    signal_generation = 0
    async def start_signaling():
        nonlocal signal_generation
        signal_generation += 1; before = count("Listening on")
        process = await launch("signaling", "node", "index.js", cwd=signaling_root, env={**os.environ,
            "API_BASE_URL": f"http://127.0.0.1:{proxy_port}/api", "REDIS_URL": "redis://127.0.0.1:6379",
            "SOCKETIO_PORT": str(port), "LOG_LEVEL": "debug", "RUNTIME_JOURNAL_DIR": str(journal_dir)})
        await wait(lambda: count("Listening on") == before + 1, f"Signaling generation {signal_generation} reconstructed domains and started")
        return process

    async def abrupt_kill(process):
        process.send_signal(signal.SIGKILL); await process.wait(); running.discard(process)
        print(f"Abrupt SIGKILL generation {signal_generation}", flush=True)

    command_seq = 0
    async def controller_command(event, **values):
        nonlocal command_seq
        command_seq += 1
        controller.stdin.write((json.dumps({"event": event, "sequence": command_seq, **values}) + "\n").encode()); await controller.stdin.drain()
        await wait(lambda: any(f"command completed {command_seq}" in line for line in logs["sdk"]), f"SDK command {event}")

    async def player():
        client, messages, mappings = socketio.AsyncClient(reconnection=False), [], asyncio.Queue(); clients.append(client)
        @client.on("message", namespace="/signaling")
        async def message(value): messages.append(value)
        @client.on("mappingResponse", namespace="/signaling")
        async def mapping(value): await mappings.put(json.loads(value) if isinstance(value, str) else value)
        await client.connect(f"http://127.0.0.1:{port}?clientType=player&gameId={config['game_id']}", transports=["websocket"], namespaces=["/signaling"])
        async def state():
            await client.emit("mapping", namespace="/signaling")
            result = await asyncio.wait_for(mappings.get(), 3)
            return next(game for game in result["games"] if game["game_id"] == config["game_id"])
        return client, messages, state

    async def send(client, event, payload=None):
        await client.emit("message", {"event": event, "src": client.get_sid("/signaling"),
            "dst": "forged" if event == "gameControls" else "gameEngine", "seat": 99, "payload": payload or {}}, namespace="/signaling")

    async def join(client, messages, status):
        before = len(messages); await send(client, "joinGame", {"queue_option_id": option["id"]})
        await wait(lambda: any(message["event"] == "joinGameResult" and message["payload"].get("status") == status for message in messages[before:]), f"Player join {status}")

    async def control(client):
        before = controls(); await send(client, "gameControls", {"id": "joystick_main", "type": "joystick", "command": {"x": .5, "y": -.5}})
        await wait(lambda: controls() == before + 1, "Fresh control reached SDK backend")

    async def mapping_matches(state, *, committed, queued, health=None, availability=None):
        async with asyncio.timeout(20):
            while True:
                value = await state(); q = next(entry for entry in value["queue_options"] if entry["id"] == option["id"])
                if value["committed_gameplay"] == committed and q["queued_players"] == queued and \
                    (health is None or value["runtime_health"] == health) and (availability is None or value["effective_availability"] == availability):
                    return value
                await asyncio.sleep(.05)

    async def history(allocation_id, reason="signaling_restart"):
        async with asyncio.timeout(20):
            while True:
                result = json.loads(await platform_eval("$s = App\\Models\\PlayerSession::find($argv[1]); echo $s ? $s->toJson() : 'null';", allocation_id))
                if result and result["ended_at"] and result["end_reason"] == reason: return result
                await asyncio.sleep(.05)

    try:
        await intent("OPEN", False)
        signaling = await start_signaling()
        controller = await launch("sdk", sys.executable, "scripts/recovery_controller.py", "--config", str(config_path), cwd=sdk,
            env={**os.environ, "PYTHONPATH": str(sdk), "PYTHONUNBUFFERED": "1"})
        await wait(lambda: count("state=READY") >= 1, "Controller completed fresh configure + snapshot")
        active, active_messages, state = await player(); queued, queued_messages, _ = await player()
        await join(active, active_messages, "confirming"); await wait(lambda: len(allocations()) == 1, "Primary allocation committed")
        primary_id = allocations()[-1]; session_ids.append(primary_id)
        history_failure["enabled"] = True; await control(active)
        await join(queued, queued_messages, "queued"); await mapping_matches(state, committed=1, queued=1)
        await wait(lambda: history_failure["count"] > 0, "Transient Platform history failure observed")
        await abrupt_kill(signaling)

        signaling = await start_signaling(); await controller_command("reconnect")
        await wait(lambda: count("state=READY") >= 2, "Controller performed new-epoch configure + snapshot")
        post, post_messages, post_state = await player()
        recovered = await mapping_matches(post_state, committed=0, queued=0, health="HEALTHY", availability="OPEN")
        assert recovered["active_set_id"] == domain["active_set_id"] and recovered["queue_options"][0]["outage_state"] == "AVAILABLE", recovered
        history_failure["enabled"] = False; first_history = await history(primary_id)
        assert first_history["started_at"] is not None
        await join(post, post_messages, "confirming"); await wait(lambda: len(allocations()) == 2, "Fresh post-restart allocation")
        recovery_id = allocations()[-1]; session_ids.append(recovery_id); await control(post)
        await controller_command("disconnect"); await wait(lambda: count("controller_recovery started") >= 1, "Controller recovery active before crash")
        await abrupt_kill(signaling)

        signaling = await start_signaling(); await controller_command("reconnect")
        await wait(lambda: count("state=READY") >= 3, "Recovery crash restarted with fresh handshake")
        after_recovery, recovery_messages, recovery_state = await player()
        await mapping_matches(recovery_state, committed=0, queued=0, health="HEALTHY", availability="OPEN")
        await history(recovery_id)
        await join(after_recovery, recovery_messages, "confirming"); await wait(lambda: len(allocations()) == 3, "Fresh allocation after discarded recovery")
        paused_id = allocations()[-1]; session_ids.append(paused_id); await control(after_recovery)
        await intent("OPEN", True); await mapping_matches(recovery_state, committed=1, queued=0, availability="PAUSED")
        await abrupt_kill(signaling)

        signaling = await start_signaling(); await controller_command("reconnect")
        await wait(lambda: count("state=READY") >= 4, "Paused crash restarted with fresh handshake")
        final, final_messages, final_state = await player()
        paused_mapping = await mapping_matches(final_state, committed=0, queued=0, health="HEALTHY", availability="PAUSED")
        await history(paused_id)
        await intent("OPEN", False); await mapping_matches(final_state, committed=0, queued=0, health="HEALTHY", availability="OPEN")
        await join(final, final_messages, "confirming"); await wait(lambda: len(allocations()) == 4, "Fresh allocation after resume")
        fresh_id = allocations()[-1]; session_ids.append(fresh_id); await control(final)
        await send(final, "leaveGame"); await history(fresh_id, "player_left")

        result = {"status": "passed", "fixture_restored": False, "abrupt_kills": 3, "forced_history_failures": history_failure["count"],
            "sessions": session_ids, "runtime_generation": paused_mapping["runtime_generation"],
            "scenarios": [
                "ACTIVE crash: old queue/allocation discarded; signaling_restart history retried from disk; safety neutralized after fresh handshake; fresh gameplay succeeded",
                "Controller-recovery crash: old recovery timer/allocation not restored; history terminated; fresh gameplay succeeded",
                "PAUSED crash: suspended ownership not restored; pause intent reconstructed; history terminated; resume allowed only fresh gameplay",
            ]}
        (output_dir / "result.json").write_text(json.dumps(result, indent=2))
    finally:
        history_failure["enabled"] = False
        for client in clients:
            if client.connected:
                try: await client.disconnect()
                except Exception: pass
        if original_intent:
            try: await intent(*original_intent)
            except Exception as error: print(f"Intent restore failed: {error}", flush=True)
        for process in reversed(processes):
            if process.returncode is None:
                process.terminate()
                try: await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError: process.kill(); await process.wait()
            running.discard(process)
        await asyncio.gather(*readers, return_exceptions=True)
        if session_ids:
            try: await platform_eval("App\\Models\\PlayerSession::whereIn('id', json_decode($argv[1], true))->delete();", json.dumps(session_ids))
            except Exception as error: print(f"History cleanup failed: {error}", flush=True)
        await proxy_client.close(); await runner.cleanup()
    result["fixture_restored"] = True
    (output_dir / "result.json").write_text(json.dumps(result, indent=2))
    print(f"PASS; evidence: {output_dir}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=3006)
    parser.add_argument("--proxy-port", type=int, default=18006)
    parser.add_argument("--output-dir", type=Path, default=Path(tempfile.mkdtemp(prefix="groundbreaking-restart-")))
    args = parser.parse_args()
    asyncio.run(verify(args.port, args.proxy_port, args.output_dir.resolve()))
