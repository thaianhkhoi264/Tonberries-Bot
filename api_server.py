"""
REST API server for Tonberries-Bot.

Receives training_start / training_end events POSTed by horseact_network_probe
and hands them to training_module.py. See
Z:\\Claude Projects\\HorseACT RE\\api_contract_plan.md for the payload contract.

Runs on its own port (API_PORT, default 8081) — separate from Gacha-Timer-Bot's
api_server.py, which already owns 8080 on the same Pi.
"""

import json
import logging
import os

from aiohttp import web

from bot import bot
from global_config import TRAINING_USER_DESCRIPTION_TO_ID
import training_module

api_logger = logging.getLogger("api_server")
api_logger.setLevel(logging.INFO)

try:
    import aiohttp_cors
except ImportError:
    aiohttp_cors = None
    api_logger.warning("aiohttp-cors not installed. CORS will be disabled.")

API_KEYS_FILE = "api_keys.json"


def load_api_keys() -> dict:
    """
    Loads API keys from api_keys.json. Format: {"key": "user_description"}.
    horseact_network_probe issues its own per-user key; the description here
    is just what maps that key to a Discord user via TRAINING_USER_DESCRIPTION_TO_ID.
    """
    if os.path.exists(API_KEYS_FILE):
        with open(API_KEYS_FILE, "r") as f:
            return json.load(f)
    default_keys = {"CHANGE_ME_secret_key_123": "Example API Key - REPLACE THIS"}
    with open(API_KEYS_FILE, "w") as f:
        json.dump(default_keys, f, indent=2)
    api_logger.warning(f"Created default {API_KEYS_FILE}. Please update with your own API keys!")
    return default_keys


VALID_API_KEYS = load_api_keys()


def get_user_id_from_api_key(api_key: str) -> int | None:
    description = VALID_API_KEYS.get(api_key)
    if description is None:
        return None
    return TRAINING_USER_DESCRIPTION_TO_ID.get(description)


def validate_api_key(request) -> tuple[bool, str | None, str | None]:
    """Returns (is_valid, error_message, api_key)."""
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        return False, "Missing API key. Provide via 'X-API-Key' header.", None
    if api_key not in VALID_API_KEYS:
        api_logger.warning(f"Invalid API key attempt: {api_key[:10]}...")
        return False, "Invalid API key.", None
    return True, None, api_key


def _authenticate(request) -> tuple[int | None, web.Response | None]:
    """Validates the key and resolves it to a user_id. Returns (user_id, error_response)."""
    is_valid, error_msg, api_key = validate_api_key(request)
    if not is_valid:
        return None, web.json_response({"success": False, "error": error_msg}, status=401)

    user_id = get_user_id_from_api_key(api_key)
    if user_id is None:
        return None, web.json_response(
            {"success": False, "error": "API key is not mapped to a Discord user_id — add it to TRAINING_USER_DESCRIPTION_TO_ID in local_config.py"},
            status=400,
        )
    return user_id, None


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------

def _require_fields(data: dict, fields: list[str]) -> str | None:
    missing = [f for f in fields if f not in data]
    if missing:
        return f"Missing required field(s): {', '.join(missing)}"
    return None


async def handle_training_start(request):
    user_id, err = _authenticate(request)
    if err:
        return err

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"success": False, "error": "Invalid JSON in request body"}, status=400)

    error = _require_fields(data, ["mode", "timestamp", "cardId"])
    if error:
        return web.json_response({"success": False, "error": error}, status=400)

    if data["mode"] not in ("independent", "manual"):
        return web.json_response({"success": False, "error": "'mode' must be 'independent' or 'manual'"}, status=400)

    try:
        await training_module.handle_training_start(user_id, data)
    except Exception as e:
        api_logger.error(f"Error handling training_start for user {user_id}: {e}", exc_info=True)
        return web.json_response({"success": False, "error": "Internal server error"}, status=500)

    return web.json_response({"success": True, "message": "training_start recorded"})


async def handle_training_end(request):
    user_id, err = _authenticate(request)
    if err:
        return err

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"success": False, "error": "Invalid JSON in request body"}, status=400)

    # `mode` is no longer mode-branched required data — every training_end carries the
    # full run shape (rank/stats/skills *and* factors *and* supportCards) regardless of
    # how it was played. `mode` itself is optional (the plugin includes it only when its
    # own local tracking knows it); training_module resolves it (persisted state, then
    # payload mode, then DM confirmation) rather than this layer rejecting its absence.
    error = _require_fields(
        data, ["timestamp", "cardId", "rank", "rankScore", "fans", "stats", "skills", "factors", "supportCards"]
    )
    if error:
        return web.json_response({"success": False, "error": error}, status=400)

    if "mode" in data and data["mode"] not in ("independent", "manual"):
        return web.json_response({"success": False, "error": "'mode' must be 'independent' or 'manual'"}, status=400)

    try:
        status = await training_module.handle_training_end(user_id, data)
    except Exception as e:
        api_logger.error(f"Error handling training_end for user {user_id}: {e}", exc_info=True)
        return web.json_response({"success": False, "error": "Internal server error"}, status=500)

    message = (
        "training_end recorded, but no matching training_start was found — DMed the user to confirm mode"
        if status == "pending_confirmation" else "training_end recorded"
    )
    return web.json_response({"success": True, "message": message})


async def handle_training_abandoned(request):
    user_id, err = _authenticate(request)
    if err:
        return err

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"success": False, "error": "Invalid JSON in request body"}, status=400)

    # `mode` and `cardId` are both best-effort here and may be absent entirely —
    # unlike training_end there's no fallback data source in the payload itself
    # for an abandoned run; training_module falls back to persisted training_start
    # state for whatever's missing. Only `timestamp` is treated as required.
    error = _require_fields(data, ["timestamp"])
    if error:
        return web.json_response({"success": False, "error": error}, status=400)

    if "mode" in data and data["mode"] not in ("independent", "manual"):
        return web.json_response({"success": False, "error": "'mode' must be 'independent' or 'manual'"}, status=400)

    try:
        await training_module.handle_training_abandoned(user_id, data)
    except Exception as e:
        api_logger.error(f"Error handling training_abandoned for user {user_id}: {e}", exc_info=True)
        return web.json_response({"success": False, "error": "Internal server error"}, status=500)

    return web.json_response({"success": True, "message": "training_abandoned recorded"})


async def handle_training_progress(request):
    user_id, err = _authenticate(request)
    if err:
        return err

    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"success": False, "error": "Invalid JSON in request body"}, status=400)

    # Manual only, per the plan doc — no fallback/resolution needed the way
    # training_end's mode is optional; this event simply doesn't fire for
    # Independent Training, so it's required and strict here.
    error = _require_fields(
        data,
        ["mode", "timestamp", "singleModeCharaId", "cardId", "scenarioId", "turn",
         "vital", "maxVital", "fans", "stats", "skillPoint"],
    )
    if error:
        return web.json_response({"success": False, "error": error}, status=400)

    if data["mode"] != "manual":
        return web.json_response({"success": False, "error": "'mode' must be 'manual' for training_progress"}, status=400)

    try:
        await training_module.handle_training_progress(user_id, data)
    except Exception as e:
        api_logger.error(f"Error handling training_progress for user {user_id}: {e}", exc_info=True)
        return web.json_response({"success": False, "error": "Internal server error"}, status=500)

    return web.json_response({"success": True, "message": "training_progress recorded"})


async def handle_health_check(request):
    return web.json_response({
        "status": "ok",
        "bot_connected": bot.is_ready() if bot else False,
        "bot_user": str(bot.user) if (bot and bot.user) else "Not connected",
    })


def create_app():
    app = web.Application()

    if aiohttp_cors:
        cors = aiohttp_cors.setup(app, defaults={
            "*": aiohttp_cors.ResourceOptions(
                allow_credentials=True,
                expose_headers="*",
                allow_headers="*",
                allow_methods=["GET", "POST"],
            )
        })
    else:
        cors = None

    app.router.add_post("/api/horseact/training_start", handle_training_start)
    app.router.add_post("/api/horseact/training_end", handle_training_end)
    app.router.add_post("/api/horseact/training_abandoned", handle_training_abandoned)
    app.router.add_post("/api/horseact/training_progress", handle_training_progress)
    app.router.add_get("/api/health", handle_health_check)

    if cors:
        for route in list(app.router.routes()):
            cors.add(route)

    return app


async def start_api_server(host="0.0.0.0", port=8081):
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    api_logger.info(f"API Server started on http://{host}:{port}")
    api_logger.info("Endpoints available:")
    api_logger.info(f"  POST http://{host}:{port}/api/horseact/training_start")
    api_logger.info(f"  POST http://{host}:{port}/api/horseact/training_end")
    api_logger.info(f"  POST http://{host}:{port}/api/horseact/training_abandoned")
    api_logger.info(f"  POST http://{host}:{port}/api/horseact/training_progress")
    api_logger.info(f"  GET  http://{host}:{port}/api/health")
    api_logger.info(f"API keys loaded from {API_KEYS_FILE}")

    return runner
