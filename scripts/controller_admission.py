"""Development-fixture admission helper for live verification scripts."""

DEVELOPMENT_CLIENT_ID = "dev-controller-001"
DEVELOPMENT_GAME_ID = "1"
DEVELOPMENT_CREDENTIAL = "groundbreaking-local-controller-token"


async def development_runtime_config(session, api_base="http://127.0.0.1:8000/api"):
    async with session.post(
        f"{api_base}/ge/v2/controller-admissions",
        json={
            "client_id": DEVELOPMENT_CLIENT_ID,
            "game_id": DEVELOPMENT_GAME_ID,
            "credential": DEVELOPMENT_CREDENTIAL,
        },
    ) as response:
        response.raise_for_status()
        return (await response.json())["runtime_config"]
