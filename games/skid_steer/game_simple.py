# games/skid_steer/game_simple.py

import argparse
from surrortg.game import Game
from games.skid_steer.skid_steer import Skidsteer
import asyncio
import logging

class SkidSimpleGame(Game):
    async def on_config(self):
        """Handle game configuration"""
        logging.info("Processing game configuration")
        set_num = await self.skid_steer.handle_config(self.configs)
        return set_num

    async def on_init(self):
        """Initialize the robot"""
        self.skid_steer = Skidsteer(self.io)
        self.io.register_inputs(self.skid_steer.inputs)

        logging.info("Starting battery polling task.")
        asyncio.create_task(self._poll_battery())

    async def on_pre_game(self):
        pass

    async def on_countdown(self):
        pass

    async def on_start(self):
        pass

    async def _poll_battery(self):
        """Request battery status every 10 seconds and send it to the signaling server"""
        while True:
            for seat in self.skid_steer.endpoints:
                # This just sends the request - response comes asynchronously
                await self.skid_steer.request_battery_status(seat)
                
            # Data will be sent via _handle_battery_response when received
            await asyncio.sleep(10)

    async def on_run(self):
        """Main game loop"""
        logging.info("Game is running.")
        while True:
            await asyncio.sleep(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser("SkidSteer Game")
    parser.add_argument(
        "-c",
        "--conf",
        metavar="",
        help="path to configuration .toml file",
        required=False,
    )
    args = parser.parse_args()
    if args.conf is not None:
        SkidSimpleGame().run(config_path=args.conf)
    else:
        SkidSimpleGame().run()
