"""
Public read-only API for the live payout feed (for the website).

Runs inside the bot on Railway's PORT. Serves the same payouts the Discord feed shows:
amount, network, coin, time, size tier and a link to the transaction. It never returns
recipient wallets, names, emails or anything from tickets.

Endpoints
  GET /api/health                     -> {"ok": true, "ready": bool}
  GET /api/payouts?limit=&before=&network=&min_usd=
                                      -> newest first, paginated with `before`
  GET /api/stats?period=all|1d|7d|30d|90d
"""

import logging
import os
import time

from aiohttp import web

from payout_feed import MIN_PAYOUT_USD, NETWORK_ICONS, NETWORKS, TIERS

log = logging.getLogger("web-api")

ALLOWED_ORIGINS = [o.strip() for o in os.getenv("WEB_ALLOWED_ORIGINS", "*").split(",") if o.strip()]
PERIOD_DAYS = {"all": None, "1d": 1, "7d": 7, "30d": 30, "90d": 90}
MAX_LIMIT = 100


# ----------------------------------------------------------------- pure logic (easy to test)
def tier_of(usd):
    for minimum, colour, label in TIERS:
        if (usd or 0) >= minimum:
            return {"label": label.title(), "key": label.split()[0].lower(), "colour": f"#{colour:06X}",
                    "min_usd": minimum}
    minimum, colour, label = TIERS[-1]
    return {"label": label.title(), "key": label.split()[0].lower(), "colour": f"#{colour:06X}", "min_usd": minimum}


def public_payouts(index: dict) -> list[dict]:
    """Every payout as safe public data, newest first. No recipient wallets."""
    out = []
    for p in index.get("payouts", {}).values():
        usd = p.get("usd")
        if usd is None or usd < MIN_PAYOUT_USD:
            continue
        net = NETWORKS.get(p["chain"], {"name": p["chain"], "explorer": ""})
        out.append({
            "id": p["hash"],
            "amount_usd": round(usd, 2),
            "asset": p["asset"],
            "asset_amount": p["amount"],
            "network": p["chain"],
            "network_name": net["name"],
            "network_icon": NETWORK_ICONS.get(p["chain"]),
            "timestamp": p["ts"],
            "tx_url": net["explorer"] + p["hash"],
            "tier": tier_of(usd),
        })
    out.sort(key=lambda x: (x["timestamp"], x["id"]), reverse=True)
    return out


def page(payouts: list[dict], limit=50, before=None, network=None, min_usd=None) -> dict:
    items = payouts
    if network:
        items = [p for p in items if p["network"] == network]
    if min_usd is not None:
        items = [p for p in items if p["amount_usd"] >= min_usd]
    if before is not None:
        items = [p for p in items if p["timestamp"] < before]
    limit = max(1, min(MAX_LIMIT, limit))
    chunk = items[:limit]
    return {
        "payouts": chunk,
        "next_before": chunk[-1]["timestamp"] if len(items) > limit else None,
    }


def stats(payouts: list[dict], period="all", now=None) -> dict:
    days = PERIOD_DAYS.get(period, None)
    now = now or time.time()
    items = [p for p in payouts if days is None or p["timestamp"] >= now - days * 86400]
    total = sum(p["amount_usd"] for p in items)
    by_net = {}
    for p in items:
        n = by_net.setdefault(p["network"], {"network_name": p["network_name"], "count": 0, "total_usd": 0.0})
        n["count"] += 1
        n["total_usd"] = round(n["total_usd"] + p["amount_usd"], 2)
    return {
        "period": period if period in PERIOD_DAYS else "all",
        "total_usd": round(total, 2),
        "count": len(items),
        "largest_usd": max((p["amount_usd"] for p in items), default=0.0),
        "average_usd": round(total / len(items), 2) if items else 0.0,
        "since": min((p["timestamp"] for p in items), default=None),
        "latest": max((p["timestamp"] for p in items), default=None),
        "by_network": by_net,
    }


# ----------------------------------------------------------------- HTTP
def _cors(request, resp):
    origin = request.headers.get("Origin")
    if "*" in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = "*"
    elif origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def _json(request, data, status=200, cache=15):
    resp = web.json_response(data, status=status)
    resp.headers["Cache-Control"] = f"public, max-age={cache}"
    return _cors(request, resp)


def _num(value, cast, default=None):
    try:
        return cast(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def make_app(feed) -> web.Application:
    cache = {"at": 0.0, "count": -1, "payouts": []}

    def current():
        # rebuild the public list only when the index changed (cheap on every request)
        n = len(feed.index.get("payouts", {}))
        if n != cache["count"] or time.time() - cache["at"] > 30:
            cache.update(at=time.time(), count=n, payouts=public_payouts(feed.index))
        return cache["payouts"]

    async def health(request):
        return _json(request, {"ok": True, "ready": bool(feed.index_ready)}, cache=0)

    async def payouts(request):
        q = request.query
        network = q.get("network") or None
        if network and network not in NETWORKS:
            return _json(request, {"error": f"network must be one of {sorted(NETWORKS)}"}, status=400, cache=0)
        data = page(current(), limit=_num(q.get("limit"), int, 50), before=_num(q.get("before"), int),
                    network=network, min_usd=_num(q.get("min_usd"), float))
        data["ready"] = bool(feed.index_ready)
        return _json(request, data)

    async def stats_view(request):
        data = stats(current(), request.query.get("period", "all"))
        data["ready"] = bool(feed.index_ready)
        return _json(request, data, cache=30)

    async def options(request):
        return _cors(request, web.Response(status=204))

    app = web.Application()
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/payouts", payouts)
    app.router.add_get("/api/stats", stats_view)
    app.router.add_route("OPTIONS", "/api/{tail:.*}", options)
    return app


async def start(feed):
    """Start the API on Railway's PORT (does nothing if PORT isn't set)."""
    port = os.getenv("PORT")
    if not port:
        log.info("Web API off (no PORT set)")
        return
    runner = web.AppRunner(make_app(feed), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    log.info("Web API listening on port %s", port)
