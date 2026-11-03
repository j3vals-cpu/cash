"""Trade-account risk review: parse trades (admin export text or API JSON), compute the numbers, flag patterns."""
import re
import statistics
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    NY = None

ROW_RE = re.compile(r"^(LONG|SHORT)\t")


def _num(s):
    s = (s or "").replace("$", "").replace(",", "").replace("×", "").replace("x", "").strip()
    if s in ("", "—", "-", "— / —"):
        return 0.0
    neg = s.startswith("-") or s.startswith("−")
    v = float(s.lstrip("-−"))
    return -v if neg else v


def _dt(s):
    s = (s or "").strip()
    for fmt in ("%d/%m/%Y, %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            d = datetime.strptime(s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def parse_export(text: str) -> list[dict]:
    """The Trades table copied from the Cashed admin panel (market line, id line, tab-separated row)."""
    trades, market = [], "?"
    for raw in (text or "").splitlines():
        line = raw.strip("\r")
        if not line.strip():
            continue
        if "Perpetual" in line or re.fullmatch(r"[A-Z0-9]+-USD.*", line.strip()):
            market = line.strip().replace(" Perpetual", "")
            continue
        if not ROW_RE.match(line):
            continue
        f = line.split("\t")
        if len(f) < 11:
            continue
        o, c = _dt(f[9]), _dt(f[10])
        trades.append({"market": market, "side": f[0], "status": f[1], "size": _num(f[2]), "leverage": f[3],
                       "entry": _num(f[4]), "close": _num(f[5]), "fees": _num(f[7]), "pnl": _num(f[8]),
                       "opened": o, "closed": c})
    return trades


def from_api(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows or []:
        out.append({"market": str(r.get("market", "?")).replace(" Perpetual", ""), "side": str(r.get("side", "")).upper(),
                    "status": r.get("status", ""), "size": float(r.get("size_usd") or r.get("size") or 0),
                    "leverage": str(r.get("leverage", "")), "entry": float(r.get("entry") or 0),
                    "close": float(r.get("close") or 0), "fees": float(r.get("fees") or 0),
                    "pnl": float(r.get("pnl") or 0), "opened": _dt(str(r.get("opened_at", ""))),
                    "closed": _dt(str(r.get("closed_at", "")))})
    return out


def _session_tag(d):
    """Where a UTC time sits relative to the US stock session (NY 9:30-16:00)."""
    if d is None or NY is None:
        return "?"
    ny = d.astimezone(NY)
    if ny.weekday() >= 5:
        return "weekend"
    mins = ny.hour * 60 + ny.minute
    if 9 * 60 + 30 <= mins < 9 * 60 + 50:
        return "us_open"          # first 20 minutes
    if 15 * 60 + 50 <= mins < 16 * 60 + 20:
        return "us_close"         # 10 min either side of the close... and just after
    if 4 * 60 <= mins < 9 * 60 + 30:
        return "premarket"
    if 16 * 60 + 20 <= mins < 20 * 60:
        return "afterhours"
    if 9 * 60 + 50 <= mins < 15 * 60 + 50:
        return "regular"
    return "overnight"


def analyse(trades: list[dict]) -> dict:
    trades = sorted([t for t in trades if t["opened"]], key=lambda t: t["opened"])
    n = len(trades)
    if not n:
        return {"n": 0}
    for t in trades:
        t["hold"] = (t["closed"] - t["opened"]).total_seconds() if t["closed"] else None
        t["session"] = _session_tag(t["opened"])
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    net = round(sum(t["pnl"] for t in trades), 2)
    holds = [t["hold"] for t in trades if t["hold"] is not None]
    charged = [t for t in trades if t["fees"] > 0]
    rate = statistics.median(t["fees"] / t["size"] for t in charged if t["size"]) if charged else 0.0
    missing = round(sum(t["size"] * rate for t in trades if t["fees"] == 0), 2) if rate else 0.0
    days = {}
    for t in trades:
        days.setdefault(t["opened"].strftime("%Y-%m-%d"), []).append(t)
    day_rows = [{"day": d, "trades": len(v), "pnl": round(sum(x["pnl"] for x in v), 2),
                 "from": v[0]["opened"].strftime("%H:%M"), "to": v[-1]["opened"].strftime("%H:%M"),
                 "sessions": sorted({x["session"] for x in v})} for d, v in sorted(days.items())]
    best_trade = max(trades, key=lambda t: t["pnl"])
    best_day = max(day_rows, key=lambda d: d["pnl"])
    flips = sum(1 for a, b in zip(trades, trades[1:])
                if a["side"] != b["side"] and a["closed"] and (b["opened"] - a["closed"]).total_seconds() <= 15)
    edge = [t for t in trades if t["session"] in ("us_open", "us_close")]
    markets = {}
    for t in trades:
        markets[t["market"]] = markets.get(t["market"], 0) + 1
    cum, low, peak, dd = 0.0, 0.0, 0.0, 0.0
    for t in trades:
        cum += t["pnl"]; low = min(low, cum); peak = max(peak, cum); dd = max(dd, peak - cum)
    return {
        "n": n, "net": net, "wins": len(wins), "losses": len(losses),
        "win_rate": round(100 * len(wins) / n), "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0,
        "volume": round(sum(t["size"] for t in trades)), "size_min": round(min(t["size"] for t in trades)),
        "size_max": round(max(t["size"] for t in trades)),
        "hold_median": round(statistics.median(holds)) if holds else None, "hold_min": round(min(holds)) if holds else None,
        "hold_max": round(max(holds)) if holds else None, "under_20s": sum(1 for h in holds if h < 20),
        "under_60s": sum(1 for h in holds if h < 60),
        "fees_total": round(sum(t["fees"] for t in trades), 2), "fees_charged": len(charged),
        "fee_rate_pct": round(rate * 100, 4), "fees_missing_est": missing,
        "net_if_fees": round(net - missing, 2),
        "best_trade": round(best_trade["pnl"], 2), "best_trade_share": round(100 * best_trade["pnl"] / net) if net > 0 else None,
        "best_day": best_day["day"], "best_day_pnl": best_day["pnl"],
        "best_day_share": round(100 * best_day["pnl"] / net) if net > 0 else None,
        "flips_15s": flips, "edge_trades": len(edge), "edge_pnl": round(sum(t["pnl"] for t in edge), 2),
        "sessions": {s: sum(1 for t in trades if t["session"] == s) for s in sorted({t["session"] for t in trades})},
        "markets": markets, "days": day_rows, "worst_point": round(low, 2), "max_drawdown": round(dd, 2),
        "biggest_loss": round(min(t["pnl"] for t in trades), 2),
        "first": trades[0]["opened"].strftime("%d %b %Y %H:%M UTC"), "last": trades[-1]["opened"].strftime("%d %b %Y %H:%M UTC"),
    }


def compact(trades: list[dict], limit=120) -> str:
    rows = []
    for t in sorted([t for t in trades if t["opened"]], key=lambda t: t["opened"])[:limit]:
        rows.append(f"{t['opened']:%d/%m %H:%M:%S} {t['market']} {t['side']} ${t['size']:,.0f} {t['entry']}->{t['close']} "
                    f"hold {int(t['hold']) if t.get('hold') is not None else '?'}s fee {t['fees']:.2f} pnl {t['pnl']:.2f} [{t.get('session')}]")
    return "\n".join(rows)
