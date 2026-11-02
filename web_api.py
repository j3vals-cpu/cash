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

import hmac
import logging
import os
import time

from aiohttp import web

from payout_feed import MIN_PAYOUT_USD, NETWORK_ICONS, NETWORKS, TIERS

log = logging.getLogger("web-api")

OFFICE_KEY = os.getenv("OFFICE_KEY", "").strip()  # private key for the office screen (/office?key=...)
OFFICE_PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "office.html")
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


def _office_ok(request) -> bool:
    key = request.query.get("key") or request.headers.get("X-Office-Key") or ""
    return bool(OFFICE_KEY) and hmac.compare_digest(key.encode(), OFFICE_KEY.encode())


def make_app(feed, office=None) -> web.Application:
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

    # ---- office screen (private: needs ?key=OFFICE_KEY)
    async def office_page(request):
        if not _office_ok(request):
            return web.Response(status=403, text="Wrong or missing key.")
        with open(OFFICE_PAGE, encoding="utf-8") as f:
            html = f.read()
        resp = web.Response(text=html, content_type="text/html")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Robots-Tag"] = "noindex"
        return resp

    async def office_data(request):
        if not _office_ok(request) or office is None:
            return web.json_response({"error": "unauthorized"}, status=403)
        try:
            data = await office.get()
        except Exception:
            log.exception("Office data failed")
            return web.json_response({"error": "unavailable"}, status=503)
        resp = web.json_response(data)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    async def office_todo_done(request):
        if not _office_ok(request) or office is None:
            return web.json_response({"error": "unauthorized"}, status=403)
        try:
            body = await request.json()
            ok = office.todo_done(int(body.get("id")))
        except Exception:
            ok = False
        return web.json_response({"ok": ok}, status=200 if ok else 404)

    app = web.Application(client_max_size=6 * 1024 * 1024)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/payouts", payouts)
    app.router.add_get("/api/stats", stats_view)
    app.router.add_route("OPTIONS", "/api/{tail:.*}", options)
    async def office_setup(request):  # the Pi setup script (no secrets in it - the key is passed when you run it)
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup_office_pi.sh")
        with open(path, encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/plain")

    async def office_quick_setup(request):  # short code so the Pi installer is quick to type: curl -sL <base>/pi/<code> | bash
        import hashlib
        code = hashlib.sha256(("pi-setup:" + OFFICE_KEY).encode()).hexdigest()[:6] if OFFICE_KEY else ""
        if not code or not hmac.compare_digest(request.match_info.get("code", "").lower().encode(), code.encode()):
            return web.Response(status=404, text="Not found")
        base = f"{request.scheme}://{request.host}"
        if request.headers.get("X-Forwarded-Proto") == "https" or "railway.app" in request.host:
            base = f"https://{request.host}"
        script = (f'#!/bin/bash\ncurl -sL "{base}/office/setup.sh" | bash -s "{base}/office?key={OFFICE_KEY}"\n')
        return web.Response(text=script, content_type="text/plain")

    # ---- office speaker (the Pi's announcer script polls these)
    async def office_rundown(request):
        if not _office_ok(request) or office is None:
            return web.json_response({"error": "unauthorized"}, status=403)
        try:
            return web.json_response({"text": await office.boot_summary()})
        except Exception:
            log.exception("Rundown failed")
            return web.json_response({"error": "unavailable"}, status=503)

    async def office_rundown_now(request):  # touchscreen button: queue a rundown for the speaker
        if not _office_ok(request) or office is None:
            return web.json_response({"error": "unauthorized"}, status=403)
        try:
            await office.queue_rundown()
            return web.json_response({"ok": True})
        except Exception:
            log.exception("Rundown (button) failed")
            return web.json_response({"error": "unavailable"}, status=503)

    async def office_voice(request):
        if not _office_ok(request) or office is None:
            return web.json_response({"error": "unauthorized"}, status=403)
        after = _num(request.query.get("after"), int, -1)
        items = office.announcements(-1)
        last = max((i["id"] for i in items), default=0)
        quiet = office.is_quiet() if hasattr(office, "is_quiet") else False
        if after < 0:  # first call after the Pi starts: don't replay old announcements
            return web.json_response({"items": [], "last": last, "quiet": quiet})
        return web.json_response({"items": [i for i in items if i["id"] > after], "last": last, "quiet": quiet})

    async def office_listen(request):  # "Hey Ultra ..." audio from the Pi
        if not _office_ok(request) or office is None:
            return web.json_response({"error": "unauthorized"}, status=403)
        audio = await request.read()
        text = (request.query.get("text") or "")[:500]
        if (not audio and not text) or len(audio) > 5_000_000:
            return web.json_response({"error": "no audio"}, status=400)
        try:
            return web.json_response(await office.voice_command(audio, text))
        except Exception as e:
            log.exception("Voice command failed")
            return web.json_response({"heard": "", "reply": "Sorry, voice control isn't working right now."}, status=200)

    async def office_state_get(request):  # the screen polls this every second for the "listening" graphic
        if not _office_ok(request) or office is None:
            return web.json_response({"error": "unauthorized"}, status=403)
        resp = web.json_response(office.get_voice_state())
        resp.headers["Cache-Control"] = "no-store"
        return resp

    async def office_state_set(request):  # the Pi's listener reports listening / thinking / speaking / idle
        if not _office_ok(request) or office is None:
            return web.json_response({"error": "unauthorized"}, status=403)
        q = request.query
        office.set_voice_state(q.get("state", "idle"), q.get("text", ""), q.get("heard", ""))
        return web.json_response({"ok": True})

    async def office_tts(request):  # Ultra's voice as a WAV (screen on laptops + the Pi)
        if not _office_ok(request) or office is None:
            return web.json_response({"error": "unauthorized"}, status=403)
        try:
            body = await request.json()
        except Exception:
            body = {}
        audio = await office.tts(str(body.get("text") or request.query.get("text") or ""))
        if not audio:
            return web.json_response({"error": "voice not ready"}, status=503)
        return web.Response(body=audio, content_type="audio/wav", headers={"Cache-Control": "no-store"})

    app.router.add_post("/office/tts", office_tts)
    app.router.add_post("/office/listen", office_listen)
    app.router.add_get("/office/state", office_state_get)
    app.router.add_post("/office/state", office_state_set)
    # ---- Spotify: one-time login + music dimming while Ultra talks
    async def spotify_login(request):
        if not _office_ok(request) or office is None:
            return web.Response(status=403, text="Wrong or missing key.")
        url = office.spotify_login_url(request.query.get("key", ""))
        if not url:
            return web.Response(text="Add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in Railway first.")
        raise web.HTTPFound(url)

    async def spotify_callback(request):
        state = request.query.get("state", "")
        if not OFFICE_KEY or not hmac.compare_digest(state.encode(), OFFICE_KEY.encode()):
            return web.Response(status=403, text="Login link expired - start again from /office/spotify/login.")
        if request.query.get("error"):
            return web.Response(text=f"Spotify said: {request.query['error']}")
        try:
            await office.spotify_callback(request.query.get("code", ""))
        except Exception as e:
            log.exception("Spotify login failed")
            return web.Response(text=f"Spotify login failed: {e}")
        return web.Response(text="Spotify connected. You can close this page - try 'Hey Ultra, play some music'.")

    async def office_duck(request):
        if not _office_ok(request) or office is None:
            return web.json_response({"error": "unauthorized"}, status=403)
        await office.duck(request.query.get("on") == "1")
        return web.json_response({"ok": True})

    app.router.add_get("/office/spotify/login", spotify_login)
    app.router.add_get("/office/spotify/callback", spotify_callback)
    app.router.add_post("/office/duck", office_duck)
    app.router.add_get("/office/rundown", office_rundown)
    app.router.add_post("/office/rundown-now", office_rundown_now)
    app.router.add_get("/office/voice", office_voice)
    app.router.add_get("/office", office_page)
    app.router.add_get("/office/setup.sh", office_setup)
    app.router.add_get("/pi/{code}", office_quick_setup)

    async def office_announcer_file(request):  # the Pi's speaker script (no secrets in it)
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "office_announcer.py")
        with open(path, encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/plain")

    app.router.add_get("/office/announcer.py", office_announcer_file)

    async def office_listener_file(request):  # the Pi's "Hey Ultra" listener (no secrets in it)
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "office_listener.py")
        with open(path, encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/plain")

    app.router.add_get("/office/listener.py", office_listener_file)
    app.router.add_get("/office/data", office_data)
    app.router.add_post("/office/todo-done", office_todo_done)
    return app


async def start(feed, office=None):
    """Start the API on Railway's PORT (does nothing if PORT isn't set)."""
    port = os.getenv("PORT")
    if not port:
        log.info("Web API off (no PORT set)")
        return
    runner = web.AppRunner(make_app(feed, office), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    log.info("Web API listening on port %s", port)
