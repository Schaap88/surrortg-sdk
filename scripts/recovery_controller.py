"""Verification-only SDK process; stdin drives backend observations/transport.

Uses the production ControllerRuntime and backend registry with normal Socket.IO
messages. No state is injected into Signaling. No physical hardware is required.
"""
import argparse
import asyncio
import json
import logging
import sys
import tomllib
from urllib.parse import urlencode

import socketio

from surrortg.network.socket_handler import Message
from surrortg.runtime import ControllerRuntime
from surrortg.runtime.simulated_backend import SimulatedRobotBackend


async def run(config_path):
    config = tomllib.loads(config_path.read_text())
    engine = config["game_engine"]
    url = engine["url"].removesuffix("/signaling") + "?" + urlencode({
        "clientType": "controller", "clientId": config["device_id"],
        "gameId": engine["id"], "token": engine["token"],
    })
    client = socketio.AsyncClient(reconnection=False)

    async def emit(event, payload):
        await client.emit("message", {"event": event, "src": config["device_id"],
            "dst": "gameEngine", "payload": payload}, namespace="/signaling")

    runtime = ControllerRuntime(config["device_id"], emit)

    @client.on("message", namespace="/signaling")
    async def receive(message):
        await runtime.handle_message(Message.from_dict(message))

    @client.on("disconnect", namespace="/signaling")
    async def disconnected(reason=None):
        runtime.disconnect()
        logging.info("verifier > Controller transport disconnected")

    async def connect():
        await client.connect(url, transports=["websocket"], namespaces=["/signaling"])
        logging.info("verifier > Controller transport connected")

    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
    try:
        await connect()
        while line := await reader.readline():
            command = json.loads(line)
            if command["event"] == "disconnect":
                await client.disconnect()
            elif command["event"] == "reconnect":
                await connect()
            elif command["event"] == "state":
                backend = runtime.robots[str(command["robot_id"])]["backend"]
                if not isinstance(backend, SimulatedRobotBackend):
                    raise ValueError("Verifier may only change simulated backend state")
                await backend.set_state(reachable=command["reachable"], ready=command["ready"], faults=command.get("faults", []))
            else:
                raise ValueError("Unknown verifier command")
            logging.info("verifier > command completed %s", command.get("sequence"))
    finally:
        transport.close()
        if client.connected:
            await client.disconnect()
        for robot in runtime.robots.values():
            await robot["backend"].stop()


if __name__ == "__main__":
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(run(parser.parse_args().config))
