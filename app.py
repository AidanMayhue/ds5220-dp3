#Main app for magic the gathering arena player count endpoint.
import os
import time
import logging
import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from decimal import Decimal
import io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import json
from urllib.parse import quote
from datetime import datetime, timezone

from chalice import Chalice, Response, Rate

# ── App + logging ──────────────────────────────────────────────────────────────
app = Chalice(app_name="steam-player-counts")
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Configuration ──────────────────────────────────────────────────────────────
TABLE_NAME  = os.environ.get("TABLE_NAME",  "steam-player-counts")
QUICKCHART_URL = "https://quickchart.io/chart"
WINDOW_HOURS = int(os.environ.get("WINDOW_HOURS", "24"))
BUCKET_NAME = os.environ.get("BUCKET_NAME", "dp3-steam-plots")

# Games must match exactly the keys used by the ingest Lambda
GAMES = [g.strip() for g in os.environ.get("STEAM_GAMES_LIST", "CS2,Dota2,PUBG").split(",")]

# ── AWS clients ────────────────────────────────────────────────────────────────
_table  = None
def get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(TABLE_NAME)
        logger.info("DynamoDB table handle: %s", TABLE_NAME)
    return _table


# ── DynamoDB helpers ───────────────────────────────────────────────────────────

def query_recent(game: str, since_ts: int) -> list[dict]:
    """
    Return all records for `game` with timestamp >= since_ts,
    sorted ascending by timestamp.
    """
    try:
        resp = get_table().query(
            KeyConditionExpression=Key("game").eq(game) & Key("timestamp").gte(since_ts),
            ScanIndexForward=True,   # ascending time order
        )
        items = resp.get("Items", [])
        logger.info("query_recent(%s, since=%s) → %d items", game, since_ts, len(items))
        return items
    except ClientError as exc:
        logger.error("DynamoDB query failed for %s: %s", game, exc)
        return []


def get_latest(game: str) -> dict | None:
    """Return the single most-recent record for a game."""
    try:
        resp = get_table().query(
            KeyConditionExpression=Key("game").eq(game),
            ScanIndexForward=False,  # descending → newest first
            Limit=1,
        )
        items = resp.get("Items", [])
        return items[0] if items else None
    except ClientError as exc:
        logger.error("DynamoDB latest query failed for %s: %s", game, exc)
        return None


def _to_float(value) -> float:
    """DynamoDB returns Decimals; cast safely to float."""
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


# ── Plot helper ────────────────────────────────────────────────────────────────

def generate_and_upload_plot() -> str | None:
    since_ts = int(time.time()) - WINDOW_HOURS * 3600
    fig, ax = plt.subplots(figsize=(10, 5))
    has_data = False

    items = query_recent("MTGArena", since_ts)
    if items:
        timestamps = [datetime.fromtimestamp(_to_float(i["timestamp"]), tz=timezone.utc) for i in items]
        counts = [_to_float(i["player_count"]) for i in items]
        ax.plot(timestamps, counts, marker="o", markersize=3, linewidth=1.5, label="MTGArena", color="blue")
        has_data = True

    if not has_data:
        plt.close(fig)
        return None

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M", tz=timezone.utc))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate(rotation=30)
    ax.set_title(f"MTG Arena Concurrent Players — Last {WINDOW_HOURS}h", fontsize=14, fontweight="bold")
    ax.set_xlabel("Time (UTC)")
    ax.set_ylabel("Concurrent Players")
    ax.legend()
    ax.grid(True, alpha=0.3)
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    buf.seek(0)
    plt.close(fig)

    s3_key = "dp3/steam/latest.png"
    try:
        boto3.client("s3").put_object(
            Bucket=BUCKET_NAME,
            Key=s3_key,
            Body=buf.getvalue(),
            ContentType="image/png",
        )
        return f"https://{BUCKET_NAME}.s3.amazonaws.com/{s3_key}"
    except ClientError as exc:
        logger.error("S3 upload failed: %s", exc)
        return None


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Zone apex — required by the API contract."""
    return {
        "about": (
            "Tracks Steam concurrent player counts for popular games "
            f"({', '.join(GAMES)}) every 15 minutes."
        ),
        "resources": ["current", "trend", "plot", "compare"],
    }


@app.route("/current")
def current():
    """Most recent player count for every tracked game."""
    logger.info("GET /current")
    lines = []
    for game in GAMES:
        item = get_latest(game)
        if item:
            count = int(_to_float(item["player_count"]))
            ts    = int(_to_float(item["timestamp"]))
            age_min = (int(time.time()) - ts) // 60
            lines.append(f"{game}: {count:,} players (sampled {age_min}m ago)")
        else:
            lines.append(f"{game}: no data yet")

    response_text = " | ".join(lines) if lines else "No data collected yet."
    return {"response": response_text}


@app.route("/trend")
def trend():
    """24-hour average, peak, and delta (now vs 24h ago) for each game."""
    logger.info("GET /trend")
    since_ts = int(time.time()) - WINDOW_HOURS * 3600
    lines = []

    for game in GAMES:
        items = query_recent(game, since_ts)
        if len(items) < 2:
            lines.append(f"{game}: insufficient data for trend")
            continue

        counts = [_to_float(i["player_count"]) for i in items]
        avg    = int(sum(counts) / len(counts))
        peak   = int(max(counts))
        latest = int(counts[-1])
        oldest = int(counts[0])
        delta  = latest - oldest
        sign   = "▲" if delta >= 0 else "▼"

        lines.append(
            f"{game}: avg {avg:,} | peak {peak:,} | "
            f"{sign}{abs(delta):,} vs {WINDOW_HOURS}h ago"
        )

    response_text = " || ".join(lines) if lines else "No trend data available."
    return {"response": response_text}


@app.route("/plot")
def plot():
    """Return a QuickChart.io URL that renders a time-series chart."""
    logger.info("GET /plot")
    url = generate_and_upload_plot()
    if url:
        return {"response": url}
    return {"response": "Plot unavailable — not enough data collected yet."}


@app.route("/compare")
def compare():
    """Stretch goal: rank games by current player count."""
    logger.info("GET /compare")
    ranked = []
    for game in GAMES:
        item = get_latest(game)
        if item:
            ranked.append((game, int(_to_float(item["player_count"]))))

    if not ranked:
        return {"response": "No data available yet."}

    ranked.sort(key=lambda x: x[1], reverse=True)
    parts = [f"#{i+1} {g}: {c:,}" for i, (g, c) in enumerate(ranked)]
    return {"response": "Current rankings: " + " | ".join(parts)}

@app.route("/debug")
def debug():
    since_ts = int(time.time()) - WINDOW_HOURS * 3600
    items = query_recent("MTGArena", since_ts)
    return {
        "response": f"{len(items)} items found since {since_ts}",
        "first": str(items[0]) if items else "none",
        "last": str(items[-1]) if items else "none",
    }

# ── Ingest Lambda ──────────────────────────────────────────────────────────────

STEAM_URL = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
INGEST_GAMES = {
    name.strip(): int(appid.strip())
    for part in os.environ.get("STEAM_GAMES", "MTGArena:2141910").split(",")
    if ":" in part
    for name, appid in [part.strip().split(":", 1)]
}


@app.schedule(Rate(15, unit=Rate.MINUTES), name="ingest")
def ingest_handler(event):
    """Scheduled Lambda: fetch Steam player counts and write to DynamoDB."""
    logger.info("Ingest run started")
    table = get_table()
    ts = int(time.time())
    results = {"timestamp": ts, "success": [], "failed": [], "skipped": []}


    for game, appid in INGEST_GAMES.items():
        try:
            import requests as req
            r = req.get(STEAM_URL, params={"appid": appid}, timeout=8)
            r.raise_for_status()
            steam_resp = r.json().get("response", {})
            if steam_resp.get("result") != 1:
                logger.warning("Bad result code for %s", game)
                results["skipped"].append(game)
                continue
            count = int(steam_resp["player_count"])
            table.put_item(Item={
                "game": game,
                "timestamp": ts,
                "player_count": count,
                "appid": appid,
            })
            logger.info("%s: %d players", game, count)
            results["success"].append({"game": game, "player_count": count})
        except Exception as exc:
            logger.error("Failed for %s: %s", game, exc)
            results["failed"].append(game)

    logger.info("Ingest complete: %s", results)
    return results
