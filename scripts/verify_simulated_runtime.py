"""Authenticated gameplay vertical slice using real local services.

Requires running Platform (port 8000), MySQL, Redis, migrations, and
GameplayMissionSeeder. Restores temporary Robot/availability changes. Keeps
verification players and their session/ledger records as evidence.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid
from urllib.parse import unquote

import aiohttp
import socketio
from controller_admission import development_runtime_config


# Verification-only instrumentation of the actual SDK runtime. Send duplicates
# from the authenticated Controller, including after its allocation is released.
CONTROLLER = r'''
import asyncio, json, logging, os, uuid
from games.skid_steer.open_world import SkidSteerOpenWorldGame
class VerificationGame(SkidSteerOpenWorldGame):
    async def on_init(self):
        await super().on_init()
        runtime = self.io.controller_runtime
        original = runtime._emit
        async def emit(event, payload):
            await original(event, payload)
            if event == 'objective_completed' and not getattr(self, 'captured', False):
                self.captured = True
                with open(os.environ['GAMEPLAY_EVIDENCE'], 'w') as f:
                    json.dump(payload, f)
                await original(event, payload)
                async def delayed():
                    while any(b['session_id'] == payload['session_id'] for b in runtime._gameplay_bindings.values()):
                        await asyncio.sleep(.05)
                    await original(event, payload)
                    await original(event, {**payload, 'event_id': str(uuid.uuid4())})
                    logging.info('verification > delayed Controller events emitted')
                asyncio.create_task(delayed())
        runtime._emit = emit
VerificationGame().run(config_path=os.environ['VERIFICATION_CONFIG'])
'''


async def verify(port, output_dir):
    sdk = Path(__file__).resolve().parents[1]
    platform = sdk.parent / 'groundbreaking-platform'
    signaling = sdk.parent / 'groundbreaking-signaling'
    output_dir.mkdir(parents=True, exist_ok=True)
    base = 'http://127.0.0.1:8000'
    logs = {'signaling': [], 'sdk': []}
    processes, readers, clients, browsers, checks = [], [], [], [], []
    fixture = None

    async def php(code, *args):
        bootstrap = "require 'vendor/autoload.php'; $app = require 'bootstrap/app.php'; $app->make(Illuminate\\Contracts\\Console\\Kernel::class)->bootstrap(); if (!app()->isLocal()) throw new RuntimeException('Local verification only'); "
        p = await asyncio.create_subprocess_exec('php', '-r', bootstrap + code, *map(str, args), cwd=platform,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await p.communicate()
        if p.returncode:
            raise RuntimeError(stderr.decode())
        return stdout.decode()

    async def wait(predicate, label):
        async with asyncio.timeout(30):
            while not predicate():
                if any(p.returncode is not None for p in processes):
                    raise RuntimeError(f'Process exited; see {output_dir}')
                await asyncio.sleep(.05)
        checks.append(label)
        print(label, flush=True)

    def has(name, value):
        return value in ''.join(logs[name])

    async def capture(p, name):
        with (output_dir / f'{name}.log').open('w') as f:
            while line := await p.stdout.readline():
                value = line.decode(errors='replace')
                logs[name].append(value)
                f.write(value)
                f.flush()

    async def launch(name, *args, cwd, env):
        p = await asyncio.create_subprocess_exec(*args, cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        processes.append(p)
        readers.append(asyncio.create_task(capture(p, name)))

    async def send(client, event, payload=None):
        await client.emit('message', {'event': event, 'dst': 'forged-controller' if event == 'gameControls' else 'gameEngine',
                                     'seat': 99, 'payload': payload or {}}, namespace='/signaling')

    async def player():
        email, password = f'gameplay-{uuid.uuid4().hex}@groundbreaking.test', uuid.uuid4().hex
        user_id = await php("$u = App\\Models\\User::forceCreate(['name' => 'Gameplay verifier', 'email' => $argv[1], 'password' => Illuminate\\Support\\Facades\\Hash::make($argv[2]), 'email_verified_at' => now(), 'inventory' => ['fuel' => 100, 'gold' => 0, 'special_items' => []]]); echo $u->id;", email, password)
        browser = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True),
                                        headers={'Accept': 'application/json', 'Origin': base, 'Referer': base + '/'})
        browsers.append(browser)
        async def csrf():
            async with browser.get(base + '/sanctum/csrf-cookie') as response:
                response.raise_for_status()
            browser.headers['X-XSRF-TOKEN'] = unquote(next(c.value for c in browser.cookie_jar if c.key == 'XSRF-TOKEN'))
        await csrf()
        async with browser.post(base + '/login', json={'email': email, 'password': password}, allow_redirects=False) as response:
            assert response.status in (200, 204, 302), (response.status, await response.text())
        await csrf()
        async with browser.post(base + f"/games/{config['game_id']}/player-socket-token") as response:
            response.raise_for_status()
            token = (await response.json())['token']
        client, messages = socketio.AsyncClient(), []
        clients.append(client)
        @client.on('message', namespace='/signaling')
        async def receive(message):
            messages.append(message)
        await client.connect(f"http://127.0.0.1:{port}?clientType=player&gameId={config['game_id']}", auth={'token': token}, namespaces=['/signaling'])
        return client, messages, browser, user_id

    def lifecycle(messages, state):
        return next((m['payload'] for m in messages if m.get('event') == 'player.lifecycle' and m['payload']['state'] == state), None)

    try:
        async with aiohttp.ClientSession() as http:
            config = await development_runtime_config(http)
            async with http.get(base + f"/api/ge/v2/games/{config['game_id']}/runtime-domain") as response:
                response.raise_for_status()
                domain = await response.json()
        option = next(q for q in domain['queue_options'] if q['name'] == 'Standard')
        simulated = next(r for r in config['robots'] if r['implementation_kind'] == 'simulated')
        fixture = json.loads(await php("$g = App\\Models\\Game::findOrFail($argv[1]); echo json_encode(['game' => $g->only(['desired_availability', 'pause_requested']), 'robots' => $g->controllers->flatMap->robots->map(fn($r) => ['id' => $r->id, 'enabled' => $r->enabled, 'custom' => $r->custom])->values()]);", config['game_id']))
        # A single simulated Robot forces queueing; physical backends point at
        # loopback for this run so no physical equipment is contacted.
        await php("$g = App\\Models\\Game::findOrFail($argv[1]); $g->update(['desired_availability' => 'OPEN', 'pause_requested' => false]); foreach ($g->controllers->flatMap->robots as $r) { $r->enabled = (string)$r->id === $argv[2]; if ($r->implementation_kind === 'physical') $r->custom = [['name' => 'address', 'value' => '127.0.0.1']]; $r->save(); }", config['game_id'], simulated['robot_id'])
        private = json.loads(await php("echo json_encode(['runtime' => config('services.signaling_runtime.token'), 'player' => config('services.player_socket.secret')]);"))
        assert private['runtime'] and private['player'], 'Configure shared runtime/player secrets first'
        config_path = output_dir / 'srtg.toml'
        config_path.write_text((sdk / 'configs/development/srtg.example.toml').read_text().replace(':3000/', f':{port}/'))
        await launch('signaling', 'node', 'index.js', cwd=signaling, env={**os.environ,
            'API_BASE_URL': base + '/api', 'REDIS_URL': 'redis://127.0.0.1:6379', 'SOCKETIO_PORT': str(port),
            'LOG_LEVEL': 'debug', 'PLATFORM_RUNTIME_COMMAND_TOKEN': private['runtime'],
            'PLAYER_SOCKET_JWT_SECRET': private['player'], 'RUNTIME_JOURNAL_DIR': str(output_dir / 'journal')})
        await wait(lambda: has('signaling', f'Listening on {port}'), 'Real Signaling and Redis ready')
        a, am, browser, user_id = await player()
        b, bm, _, _ = await player()
        await send(a, 'joinGame', {'queue_option_id': option['id']})
        await wait(lambda: lifecycle(am, 'QUEUED'), 'Authenticated player joins Skid Steer Open World and enters queue')
        await launch('sdk', sys.executable, '-c', CONTROLLER, cwd=sdk, env={**os.environ, 'PYTHONPATH': str(sdk),
            'PYTHONUNBUFFERED': '1', 'VERIFICATION_CONFIG': str(config_path),
            'GROUNDBREAKING_DEV_COMPLETE_OBJECTIVE': '1', 'GAMEPLAY_EVIDENCE': str(output_dir / 'event.json'),
            'SURRORTG_MANAGEMENT_SOCKET': str(output_dir / 'controller.sock')})
        await wait(lambda: lifecycle(am, 'ALLOCATED'), 'Robot reserved and readiness confirmed; player receives control')
        allocation = lifecycle(am, 'ALLOCATED')
        session_id = allocation['session_id']
        await send(b, 'joinGame', {'queue_option_id': option['id']})
        await wait(lambda: lifecycle(bm, 'QUEUED'), 'Second authenticated player queues behind allocated Robot')
        await send(a, 'gameControls', {'id': 'joystick_main', 'type': 'joystick', 'command': {'x': .5, 'y': -.75}})
        await wait(lambda: lifecycle(am, 'ACTIVE') and has('sdk', 'control applied outputs='), 'Player Session starts and control reaches simulated backend')
        await wait(lambda: any(m.get('event') == 'gameplay.result' for m in am), 'SDK objective reaches Platform through Signaling; feedback reaches player')
        receipt = next(m['payload'] for m in am if m.get('event') == 'gameplay.result')
        assert receipt['status'] == 'completed' and receipt['reward']['gold'] == 25
        assert receipt['session_id'] == session_id and receipt['objective_key'] == 'first_construction_task'
        assert receipt['objective'] == 'Complete your first construction task.'
        await wait(lambda: sum('status: "completed"' in line or '"status":"completed"' in line for line in logs['signaling']) >= 2, 'Duplicate SDK event returns the committed receipt')
        captured = json.loads((output_dir / 'event.json').read_text())
        await send(a, 'objective_completed', {**captured, 'event_id': str(uuid.uuid4())})
        await wait(lambda: any(m.get('event') == 'gameplay.ack' and m['payload']['status'] == 'rejected' for m in am), 'Player-submitted completion rejected')
        await asyncio.sleep(1)
        await send(a, 'leaveGame')
        await wait(lambda: lifecycle(am, 'ENDED') and has('sdk', 'neutralization succeeded outputs='), 'Session ends; Robot neutralized and released')
        await wait(lambda: has('sdk', 'delayed Controller events emitted') and sum('status: "rejected"' in line or '"status":"rejected"' in line for line in logs['signaling']) >= 3, 'Duplicate and fresh delayed Controller events rejected')
        await wait(lambda: lifecycle(bm, 'ALLOCATED'), 'Neutralized Robot is reusable by queued player')
        await send(b, 'leaveGame')
        async with asyncio.timeout(10):
            while True:
                async with browser.get(base + f'/api/player-sessions/{session_id}/gameplay-result') as response:
                    response.raise_for_status()
                    result = await response.json()
                if result['ended_at']:
                    break
                await asyncio.sleep(.1)
        assert len(result['objectives']) == 1 and result['objectives'][0] == receipt
        async with browser.post(base + f'/api/player-sessions/{session_id}/fuel-settlements', json={'idempotency_key': f'gameplay-verification:{session_id}', 'amount': 1}) as response:
            response.raise_for_status()
            fuel = await response.json()
        assert fuel['applied_amount'] == 1 and fuel['balance'] == 99
        evidence = json.loads(await php("$s = App\\Models\\PlayerSession::findOrFail($argv[1]); echo json_encode(['session' => $s, 'inventory' => $s->user->inventory, 'events' => App\\Models\\GameplayEvent::where('player_session_id', $s->id)->count(), 'rewards' => App\\Models\\EconomyTransaction::where('player_session_id', $s->id)->where('reason', 'gameplay_objective_completed')->count(), 'fuel_settlements' => App\\Models\\EconomyTransaction::where('player_session_id', $s->id)->where('resource', 'fuel')->count()]);", session_id))
        assert evidence['session']['user_id'] == user_id
        assert str(evidence['session']['robot_id']) == simulated['robot_id']
        assert evidence['session']['started_at'] and evidence['session']['ended_at']
        assert evidence['inventory']['gold'] == 25 and evidence['inventory']['fuel'] == 99
        assert evidence['events'] == evidence['rewards'] == evidence['fuel_settlements'] == 1
        checks.append('One persisted mission result, one gold ledger credit, and one fuel settlement')
        (output_dir / 'result.json').write_text(json.dumps({'status': 'passed', 'checks': checks, 'receipt': receipt, 'fuel': fuel, 'evidence': evidence}, indent=2))
        print(f'PASS; evidence: {output_dir}', flush=True)
    finally:
        for c in clients:
            if c.connected:
                await c.disconnect()
        for p in reversed(processes):
            if p.returncode is None:
                p.terminate()
                try:
                    await asyncio.wait_for(p.wait(), 5)
                except asyncio.TimeoutError:
                    p.kill()
                    await p.wait()
        await asyncio.gather(*readers)
        for browser in browsers:
            await browser.close()
        if fixture:
            await php("$f = json_decode($argv[2], true); App\\Models\\Game::findOrFail($argv[1])->update($f['game']); foreach ($f['robots'] as $r) App\\Models\\Robot::findOrFail($r['id'])->update(['enabled' => $r['enabled'], 'custom' => $r['custom']]);", config['game_id'], json.dumps(fixture))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=3001)
    parser.add_argument('--output-dir', type=Path, default=Path(tempfile.mkdtemp(prefix='groundbreaking-gameplay-')))
    args = parser.parse_args()
    asyncio.run(verify(args.port, args.output_dir.resolve()))
