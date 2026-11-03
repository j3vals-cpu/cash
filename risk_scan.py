"""Scan every account: hedging (Rule 98 same person, Rules 100-101 between people) and short-hold /
tick-sniping review (Rules 105-106). Input: list of {account:{...}, trades:[...]} from the API, or a CSV."""
import csv
import html
import io
import statistics
from collections import defaultdict
from datetime import datetime

from risk_review import _session_tag, from_api

SIZE_MATCH = 0.8        # opposite trades count as a hedge pair when the smaller size is >= 80% of the larger
CROSS_OPEN_WINDOW = 120  # between different people: opened within 2 minutes of each other
MIN_OVERLAP = 10         # seconds both positions were open at the same time


def load_csv(text: str) -> list[dict]:
    """CSV with columns: account_id, username, discord_username, market, side, size_usd, entry, close, fees, pnl,
    opened_at, closed_at (extra columns are ignored)."""
    accounts = {}
    for r in csv.DictReader(io.StringIO(text)):
        r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
        aid = r.get("account_id") or r.get("account") or "?"
        a = accounts.setdefault(aid, {"account": {"id": aid, "username": r.get("username", ""),
                                                  "discord_username": r.get("discord_username", ""),
                                                  "phase": r.get("phase", ""), "status": r.get("account_status", "")},
                                      "trades": []})
        a["trades"].append(r)
    return list(accounts.values())


def scan(entries: list[dict], short_secs: int = 30) -> dict:
    users = defaultdict(lambda: {"accounts": set(), "discord": "", "trades": []})
    all_trades = []
    for e in entries:
        acc = e.get("account") or {}
        owner = (acc.get("username") or acc.get("owner") or acc.get("id") or "?").lstrip("@")
        u = users[owner]
        u["accounts"].add(acc.get("id") or "?")
        u["discord"] = u["discord"] or acc.get("discord_username") or ""
        for t in from_api(e.get("trades") or []):
            if not t["opened"]:
                continue
            t["owner"], t["account"] = owner, acc.get("id") or "?"
            t["hold"] = (t["closed"] - t["opened"]).total_seconds() if t["closed"] else None
            t["session"] = _session_tag(t["opened"])
            u["trades"].append(t)
            all_trades.append(t)

    # --- opposite, overlapping, similar-size positions on the same market in different accounts
    same, cross = [], []
    by_market = defaultdict(list)
    for t in all_trades:
        if t["closed"]:
            by_market[t["market"]].append(t)
    for mk, ts in by_market.items():
        ts.sort(key=lambda t: t["opened"])
        active = []
        for b in ts:
            active = [a for a in active if a["closed"] > b["opened"]]
            for a in active:
                if a["account"] == b["account"] or a["side"] == b["side"] or not a["size"] or not b["size"]:
                    continue
                if min(a["size"], b["size"]) / max(a["size"], b["size"]) < SIZE_MATCH:
                    continue
                overlap = (min(a["closed"], b["closed"]) - b["opened"]).total_seconds()
                if overlap < MIN_OVERLAP:
                    continue
                pair = {"market": mk, "a": a, "b": b, "overlap": overlap,
                        "combined": round(a["pnl"] + b["pnl"], 2), "gap": (b["opened"] - a["opened"]).total_seconds()}
                if a["owner"] == b["owner"]:
                    same.append(pair)
                elif pair["gap"] <= CROSS_OPEN_WINDOW:
                    cross.append(pair)
            active.append(b)

    rows = []
    for owner, u in users.items():
        ts = u["trades"]
        if not ts:
            continue
        holds = [t["hold"] for t in ts if t["hold"] is not None]
        short = [t for t in ts if t["hold"] is not None and t["hold"] < short_secs]
        edge = [t for t in ts if t["session"] in ("us_open", "us_close")]
        s_pairs = [p for p in same if p["a"]["owner"] == owner]
        c_pairs = [p for p in cross if owner in (p["a"]["owner"], p["b"]["owner"])]
        partners = sorted({p["b"]["owner"] if p["a"]["owner"] == owner else p["a"]["owner"] for p in c_pairs})
        n = len(ts)
        short_share = len(short) / n
        short_pnl = sum(t["pnl"] for t in short)
        score = 0
        score += 40 if s_pairs else 0
        score += 30 if c_pairs else 0
        score += 25 if short_share >= 0.5 and len(short) >= 10 else (10 if len(short) >= 5 else 0)
        score += 10 if short and short_pnl > 0 and len([t for t in short if t["pnl"] > 0]) / len(short) >= 0.65 else 0
        score += 10 if edge and len(edge) / n >= 0.4 else 0
        rows.append({
            "owner": owner, "discord": u["discord"], "accounts": len(u["accounts"]), "trades": n,
            "net": round(sum(t["pnl"] for t in ts), 2), "short": len(short), "short_share": round(100 * short_share),
            "short_pnl": round(short_pnl, 2),
            "short_win": round(100 * len([t for t in short if t["pnl"] > 0]) / len(short)) if short else 0,
            "hold_median": round(statistics.median(holds)) if holds else None,
            "edge_share": round(100 * len(edge) / n), "same_pairs": len(s_pairs), "cross_pairs": len(c_pairs),
            "partners": partners, "score": score,
        })
    rows.sort(key=lambda r: (-r["score"], -r["short"], r["owner"]))
    return {"users": rows, "same": same, "cross": cross, "short_secs": short_secs,
            "n_users": len(users), "n_trades": len(all_trades)}


def _m(v):
    return ("+" if v >= 0 else "−") + f"${abs(v):,.2f}"


def build_scan_html(res: dict, css: str) -> str:
    e = html.escape
    U, S = res["users"], res["short_secs"]
    hedge_users = [u for u in U if u["same_pairs"] or u["cross_pairs"]]
    short_users = [u for u in U if u["short"] >= 5]

    def who(u):
        d = f'<div class="n">{e(u["discord"])}</div>' if u["discord"] else '<div class="n">not linked</div>'
        return f'<td><b>@{e(u["owner"])}</b>{d}</td>'

    def pair_rows(pairs, limit=400):
        out = []
        for p in sorted(pairs, key=lambda p: p["a"]["opened"])[:limit]:
            a, b = p["a"], p["b"]
            out.append(
                f'<tr><td class="num">{a["opened"]:%d/%m %H:%M:%S}</td><td>{e(p["market"])}</td>'
                f'<td>@{e(a["owner"])}<div class="n">{e(a["account"])}</div></td><td><span class="tag">{a["side"]}</span> ${a["size"]:,.0f}</td>'
                f'<td>@{e(b["owner"])}<div class="n">{e(b["account"])}</div></td><td><span class="tag">{b["side"]}</span> ${b["size"]:,.0f}</td>'
                f'<td class="r num">{int(p["overlap"])}s</td><td class="r num">{_m(a["pnl"])} / {_m(b["pnl"])}</td></tr>')
        return "".join(out) or '<tr><td colspan="8">None found.</td></tr>'

    risk_rows = "".join(
        f'<tr>{who(u)}<td class="r num">{u["score"]}</td><td class="r num">{u["accounts"]}</td><td class="r num">{u["trades"]}</td>'
        f'<td class="r num">{u["short"]} ({u["short_share"]}%)</td><td class="r num">{u["short_win"]}%</td>'
        f'<td class="r num">{u["hold_median"] if u["hold_median"] is not None else "?"}s</td><td class="r num">{u["edge_share"]}%</td>'
        f'<td class="r num">{u["same_pairs"]}</td><td class="r num">{u["cross_pairs"]}'
        f'{("<div class=n>with @" + e(", @".join(u["partners"][:4])) + "</div>") if u["partners"] else ""}</td>'
        f'<td class="r num {"pos" if u["net"] >= 0 else "neg"}">{_m(u["net"])}</td></tr>'
        for u in U if u["score"] > 0)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>All-users risk scan</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{css} body{{margin:0}} .wrap{{max-width:1200px}} td .n{{color:var(--muted);font-size:11px}}</style></head><body><div class="wrap">
<header><h1>All-users risk scan</h1><p class="sub">{res['n_users']:,} users · {res['n_trades']:,} trades · scanned {datetime.now():%d %b %Y %H:%M} · short trade = under {S} seconds</p></header>
<section class="verdict"><b>Review list, not verdicts</b><p>Fast trading is allowed (Rules 85–86). It's only a breach when it exploits stale or delayed prices (Rules 105–106), so check those fills against the real market. Opposite trades are evidence for hedging (Rules 98, 100–101), but the rulebook needs proof they were deliberate before any action.</p></section>
<section><h2>Summary</h2><div class="stats">
<div class="stat"><div class="k">Users flagged</div><div class="v">{sum(1 for u in U if u['score'] > 0)}</div><div class="n">of {res['n_users']:,}</div></div>
<div class="stat"><div class="k">Same-person hedge pairs</div><div class="v">{len(res['same'])}</div><div class="n">Rule 98 · {sum(1 for u in U if u['same_pairs'])} users</div></div>
<div class="stat"><div class="k">Between-people pairs</div><div class="v">{len(res['cross'])}</div><div class="n">Rules 100–101</div></div>
<div class="stat"><div class="k">Users with 5+ short trades</div><div class="v">{len(short_users)}</div><div class="n">under {S} seconds</div></div>
</div></section>
<section><h2>Users to review (highest first)</h2><div class="box scroll"><table>
<thead><tr><th>User · Discord</th><th class="r">Score</th><th class="r">Accounts</th><th class="r">Trades</th><th class="r">Under {S}s</th><th class="r">Short win rate</th><th class="r">Median hold</th><th class="r">US open/close</th><th class="r">Same-person hedges</th><th class="r">Hedges with others</th><th class="r">Net P&amp;L</th></tr></thead>
<tbody>{risk_rows or '<tr><td colspan="11">Nobody flagged.</td></tr>'}</tbody></table></div></section>
<section><h2>Same person, opposite accounts (Rule 98)</h2><div class="box scroll"><table><thead><tr><th>Opened (UTC)</th><th>Market</th><th>Account A</th><th>A</th><th>Account B</th><th>B</th><th class="r">Both open</th><th class="r">P&amp;L A / B</th></tr></thead><tbody>{pair_rows(res['same'])}</tbody></table></div></section>
<section><h2>Different people, opposite trades within 2 minutes (Rules 100–101)</h2><div class="box scroll"><table><thead><tr><th>Opened (UTC)</th><th>Market</th><th>Trader A</th><th>A</th><th>Trader B</th><th>B</th><th class="r">Both open</th><th class="r">P&amp;L A / B</th></tr></thead><tbody>{pair_rows(res['cross'])}</tbody></table></div></section>
<p class="foot">Hedge pair = opposite sides of the same market in two different accounts, open at the same time for at least {MIN_OVERLAP}s, sizes within 80% of each other. Score: same-person hedge 40, hedge with someone else 30, mostly sub-{S}s trades 25 (5+ short trades 10), short trades mostly winning 10, 40%+ at the US open/close 10.</p>
</div></body></html>"""
