
import os
import time
import logging
import requests
import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
 
# ── Logging ────────────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.INFO)
 
# ── Configuration ──────────────────────────────────────────────────────────────
# Games to track: display name → Steam AppID
# Override via the STEAM_GAMES env var as "CS2:730,Dota2:570"
def _load_games() -> dict[str, int]:
    raw = os.environ.get("STEAM_GAMES", "MTGArena:2141910")
    games = {}
    for part in raw.split(","):
        part = part.strip()
        if ":" not in part:
            logger.warning("Skipping malformed STEAM_GAMES entry: %s", part)
            continue
        name, appid = part.split(":", 1)
        try:
            games[name.strip()] = int(appid.strip())
        except ValueError:
            logger.warning("Non-integer AppID for %s, skipping", name)
    logger.info("Tracking games: %s", games)
    return games
 
TABLE_NAME   = os.environ.get("TABLE_NAME", "steam-player-counts")
STEAM_URL    = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "8"))   # seconds
 
# ── AWS clients (initialised outside handler for Lambda container reuse) ───────
_dynamodb = None
 
def _get_table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb").Table(TABLE_NAME)
        logger.info("DynamoDB table handle initialised: %s", TABLE_NAME)
    return _dynamodb
 
 
# ── Core helpers ───────────────────────────────────────────────────────────────
 
def fetch_player_count(appid: int) -> int | None:
    """
    Call the Steam Web API for the current concurrent player count.
 
    Returns the integer count, or None if the request fails or Steam
    returns an unexpected payload.
    """
    try:
        resp = requests.get(
            STEAM_URL,
            params={"appid": appid},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
 
        # Steam returns {"response": {"player_count": N, "result": 1}}
        # result == 1 means success; result == 42 means the appid is invalid
        steam_resp = data.get("response", {})
        result_code = steam_resp.get("result")
        if result_code != 1:
            logger.warning("Steam result code %s for appid %s — skipping", result_code, appid)
            return None
 
        count = steam_resp.get("player_count")
        if count is None:
            logger.warning("player_count missing in Steam response for appid %s", appid)
            return None
 
        return int(count)
 
    except requests.exceptions.Timeout:
        logger.error("Timeout fetching player count for appid %s", appid)
        return None
    except requests.exceptions.RequestException as exc:
        logger.error("HTTP error for appid %s: %s", appid, exc)
        return None
    except (KeyError, ValueError, TypeError) as exc:
        logger.error("Unexpected Steam response for appid %s: %s", appid, exc)
        return None
 
 
def write_record(table, game: str, appid: int, timestamp: int, player_count: int) -> bool:
    """
    Write one timestamped sample to DynamoDB.
    Returns True on success, False on failure.
    """
    item = {
        "game":         game,
        "timestamp":    timestamp,
        "player_count": player_count,
        "appid":        appid,
    }
    try:
        table.put_item(Item=item)
        logger.info("Wrote record: %s", item)
        return True
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        logger.error(
            "DynamoDB PutItem failed for %s at %s: [%s] %s",
            game, timestamp, error_code, exc.response["Error"]["Message"]
        )
        return False
    except Exception as exc:
        logger.error("Unexpected error writing record for %s: %s", game, exc)
        return False
 
 
# ── Lambda handler ─────────────────────────────────────────────────────────────
 
def handler(event, context):
    """
    Main Lambda entry point.
 
    Fetches player counts for all configured games and persists them.
    Always returns a summary dict so CloudWatch logs show what happened.
    """
    logger.info("Ingest run started. Event: %s", event)
 
    games     = _load_games()
    table     = _get_table()
    timestamp = int(time.time())
 
    results = {"timestamp": timestamp, "success": [], "failed": [], "skipped": []}
 
    for game, appid in games.items():
        logger.info("Fetching player count for %s (appid=%s)", game, appid)
 
        player_count = fetch_player_count(appid)
        if player_count is None:
            logger.warning("No player count returned for %s — skipping write", game)
            results["skipped"].append(game)
            continue
 
        logger.info("%s: %d concurrent players", game, player_count)
 
        ok = write_record(table, game, appid, timestamp, player_count)
        if ok:
            results["success"].append({"game": game, "player_count": player_count})
        else:
            results["failed"].append(game)
 
    logger.info(
        "Ingest run complete. success=%d skipped=%d failed=%d",
        len(results["success"]),
        len(results["skipped"]),
        len(results["failed"]),
    )
    return results
