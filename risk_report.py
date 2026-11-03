"""Builds the HTML risk report (same layout as the hand-made reviews) from risk_review.analyse()."""
import html
import json

from risk_review import analyse

CSS = """
/* Layout: one reading column; verdict first, then numbers, flags, daily split, chart, full trade log */
:root{
  --bg:#f4f6f9; --panel:#ffffff; --fg:#141a24; --muted:#5b6678; --line:#dde3ec;
  --accent:#2f6fd6; --good:#13824f; --bad:#c43b3b; --warn:#a86700; --warn-bg:#fff4dc; --bad-bg:#fdecec;
  --sans:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#0f141c; --panel:#161d28; --fg:#e6ebf3; --muted:#93a0b4; --line:#263142;
  --accent:#7fb0ff; --good:#4fd194; --bad:#ff7a7a; --warn:#f2b54a; --warn-bg:#2a2214; --bad-bg:#2b1a1c; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#0f141c; --panel:#161d28; --fg:#e6ebf3; --muted:#93a0b4; --line:#263142;
  --accent:#7fb0ff; --good:#4fd194; --bad:#ff7a7a; --warn:#f2b54a; --warn-bg:#2a2214; --bad-bg:#2b1a1c; color-scheme:dark}
body{background:var(--bg);color:var(--fg);font:15px/1.55 var(--sans)}
.wrap{max-width:980px;margin:0 auto;padding-block:32px 56px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));display:grid;gap:28px}
h1{font-size:28px;line-height:1.2;margin:0;text-wrap:balance}
h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 12px;font-weight:600}
.sub{color:var(--muted);margin:6px 0 0}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.verdict{background:var(--warn-bg);border:1px solid var(--warn);border-radius:10px;padding:18px 20px;display:grid;gap:6px}
.verdict b{color:var(--warn);font-size:13px;letter-spacing:.08em;text-transform:uppercase}
.verdict p{margin:0;max-width:70ch}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.stat{background:var(--panel);padding:14px 16px;min-width:0}
.stat .k{font-size:12px;color:var(--muted)}
.stat .v{font:500 22px/1.3 var(--mono);font-variant-numeric:tabular-nums;margin-top:2px}
.stat .n{font-size:12px;color:var(--muted)}
@media (max-width:700px){.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
.flags{display:grid;gap:10px;margin:0;padding:0;list-style:none}
.flag{display:grid;grid-template-columns:auto 1fr;gap:12px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.flag .lvl{font:600 11px/1 var(--sans);letter-spacing:.06em;text-transform:uppercase;padding:5px 8px;border-radius:999px;height:fit-content;white-space:nowrap}
.hi{background:var(--bad-bg);color:var(--bad)} .md{background:var(--warn-bg);color:var(--warn)} .lo{background:var(--line);color:var(--muted)}
.flag h3{margin:0 0 2px;font-size:15px} .flag p{margin:0;color:var(--muted);max-width:75ch}
.flag div{min-width:0}
.box{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;font-weight:600;color:var(--muted);font-size:12px;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td.r,th.r{text-align:right}
tr:last-child td{border-bottom:0}
.pos{color:var(--good)} .neg{color:var(--bad)}
.tag{font:600 11px var(--sans);padding:2px 6px;border-radius:4px;border:1px solid var(--line)}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--warn);margin-right:6px;vertical-align:middle}
svg text{fill:var(--muted);font:11px var(--mono)}
.checks{margin:0;padding-left:20px;display:grid;gap:6px;max-width:75ch}
.foot{color:var(--muted);font-size:12px}
"""


def _m(v, sign=False):
    s = f"${abs(v):,.2f}"
    if sign:
        return ("+" if v >= 0 else "−") + s
    return ("−" if v < 0 else "") + s


def _dur(s):
    if s is None:
        return "?"
    s = int(s)
    return f"{s} sec" if s < 60 else f"{s // 60} min {s % 60} sec"


SESSION_NAMES = {"us_open": "US open", "us_close": "US close", "premarket": "US pre-market", "afterhours": "US after-hours",
                 "regular": "US hours", "overnight": "overnight", "weekend": "weekend", "?": "?"}


def flags_for(a: dict) -> list[dict]:
    f, n = [], a["n"]
    if a["edge_trades"] >= max(3, n * 0.5):
        f.append(("hi", "Trades cluster at the US open or close",
                  f"{a['edge_trades']} of {n} trades ({_m(a['edge_pnl'], True)} of the profit) were placed in the first 20 minutes "
                  f"after the US stock market opened or around the close. Perps that track US stocks/ETFs can lag or jump at those "
                  f"moments. Check his entry prices against the real market at those seconds."))
    elif a["edge_trades"] >= max(3, n * 0.25):
        f.append(("md", "Some trades at the US open or close",
                  f"{a['edge_trades']} of {n} trades were at the US open or close. Worth a spot-check of those fills."))
    if a["hold_median"] is not None and a["hold_median"] < 30 and a["under_60s"] >= n * 0.8:
        f.append(("hi", "Seconds-long scalps",
                  f"Median hold {_dur(a['hold_median'])}; {a['under_20s']} trades under 20 seconds and {a['under_60s']} under a minute. "
                  f"Very short holds at full size are typical of latency/price-feed trading."))
    elif a["hold_median"] is not None and a["hold_median"] < 120:
        f.append(("md", "Fast, full-size trades",
                  f"Median hold {_dur(a['hold_median'])} with ${a['size_min']:,}–${a['size_max']:,} per trade. Aggressive scalping, "
                  f"allowed by the rules unless the fills look wrong."))
    if a["flips_15s"] >= 5:
        f.append(("md", "Rapid side-flipping",
                  f"{a['flips_15s']} times he reversed direction within 15 seconds of closing a trade."))
    if a["fees_charged"] < n and a["fees_missing_est"] > 0:
        f.append(("md", "Fees missing on most trades (platform issue)",
                  f"Only {a['fees_charged']} of {n} trades were charged a fee (about {a['fee_rate_pct']}% of size). Charged on every "
                  f"trade, he'd have paid about {_m(a['fees_missing_est'])} more, so real profit is about {_m(a['net_if_fees'], True)}. "
                  f"Raise with Sam."))
    if a["best_day_share"] is not None and a["best_day_share"] > 40 and len(a["days"]) > 1:
        f.append(("md", "Profit leans on one day",
                  f"Best day ({a['best_day']}) is {a['best_day_share']}% of total profit."))
    if a["best_trade_share"] is not None and a["best_trade_share"] > 30:
        f.append(("md", "One trade carries the result",
                  f"Best trade {_m(a['best_trade'], True)} is {a['best_trade_share']}% of all profit."))
    if a["sessions"].get("weekend"):
        f.append(("lo", "Weekend trades", f"{a['sessions']['weekend']} trades at the weekend, when US stock prices don't move."))
    if not f:
        f.append(("lo", "Nothing unusual", "No timing, hold-time, fee or concentration patterns stood out."))
    return [{"level": l, "title": t, "detail": d} for l, t, d in f]


def verdict(flags):
    if any(x["level"] == "hi" for x in flags):
        return "bad", "Review before paying", "One or more high-risk patterns below. Check them before paying out."
    if any(x["level"] == "md" for x in flags):
        return "warn", "Check the flagged points", "Nothing clearly abusive, but look at the points below before paying."
    return "ok", "Looks legitimate", "No risk patterns found in these trades."


def build_html(trades: list[dict], account: dict | None = None) -> str:
    a = analyse(trades)
    if not a.get("n"):
        return "<p>No trades found.</p>"
    acc = account or {}
    flags = flags_for(a)
    vkind, vtitle, vtext = verdict(flags)
    who = acc.get("username") or ""
    accid = acc.get("id") or ""
    title = f"{'@' + who + ' · ' if who else ''}{accid + ' · ' if accid else ''}trade overview"
    markets = ", ".join(f"{k} ({v})" for k, v in sorted(a["markets"].items(), key=lambda x: -x[1]))
    acc_line = ""
    if acc:
        bits = [acc.get("phase"), acc.get("status"),
                f"start ${float(acc['starting_capital']):,.0f}" if acc.get("starting_capital") else None,
                f"balance ${float(acc['balance']):,.2f}" if acc.get("balance") is not None else None]
        acc_line = " · ".join(str(b) for b in bits if b)
    e = html.escape
    stats = [
        ("Net P&amp;L", _m(a["net"], True), f"after {_m(a['fees_total'])} fees", "pos" if a["net"] >= 0 else "neg"),
        ("Win rate", f"{a['win_rate']}%", f"{a['wins']} wins · {a['losses']} losses", ""),
        ("Avg win / loss", f"{_m(a['avg_win'])} / {_m(a['avg_loss'])}", "", ""),
        ("Volume traded", f"${a['volume']:,}", f"${a['size_min']:,}–${a['size_max']:,} per trade", ""),
        ("Median hold", _dur(a["hold_median"]), f"{_dur(a['hold_min'])} to {_dur(a['hold_max'])}", ""),
        ("Fees charged", f"{a['fees_charged']} of {a['n']}", f"{_m(a['fees_total'])} total", ""),
        ("Best trade", _m(a["best_trade"], True), f"{a['best_trade_share']}% of profit" if a["best_trade_share"] is not None else "", ""),
        ("Biggest loss", _m(a["biggest_loss"], True), f"max drawdown {_m(a['max_drawdown'])}", ""),
    ]
    stat_html = "".join(f'<div class="stat"><div class="k">{k}</div><div class="v {c}">{v}</div><div class="n">{n}</div></div>'
                        for k, v, n, c in stats)
    lv = {"hi": "High", "md": "Medium", "lo": "Note"}
    flag_html = "".join(f'<li class="flag"><span class="lvl {x["level"]}">{lv[x["level"]]}</span><div><h3>{e(x["title"])}</h3>'
                        f'<p>{e(x["detail"])}</p></div></li>' for x in flags)
    day_html = "".join(
        f'<tr><td>{d["day"]}</td><td>{d["from"]} – {d["to"]} UTC · {e(", ".join(SESSION_NAMES.get(s, s) for s in d["sessions"]))}</td>'
        f'<td class="r num">{d["trades"]}</td><td class="r num {"pos" if d["pnl"] >= 0 else "neg"}">{_m(d["pnl"], True)}</td></tr>'
        for d in a["days"])
    rows = [[t["opened"].strftime("%d/%m %H:%M:%S"), t["market"], t["side"], t["size"], t["entry"], t["close"],
             t["hold"], t["fees"], t["pnl"], SESSION_NAMES.get(t["session"], t["session"])]
            for t in sorted([t for t in trades if t["opened"]], key=lambda t: t["opened"])]
    vcls = {"bad": "", "warn": "", "ok": " ok"}[vkind]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{CSS}
body{{margin:0}} .verdict.ok{{background:var(--panel);border-color:var(--good)}} .verdict.ok b{{color:var(--good)}}</style></head><body>
<div class="wrap">
<header><h1>{e(title)}</h1>
<p class="sub">{a['n']} trades · {e(a['first'])} → {e(a['last'])} · {e(markets)}{(' · ' + e(acc_line)) if acc_line else ''}</p></header>
<section class="verdict{vcls}"><b>{e(vtitle)}</b><p>{e(vtext)}</p></section>
<section><h2>Summary</h2><div class="stats">{stat_html}</div></section>
<section><h2>What stands out</h2><ul class="flags">{flag_html}</ul></section>
<section><h2>By day</h2><div class="box scroll"><table><thead><tr><th>Day</th><th>When</th><th class="r">Trades</th><th class="r">P&amp;L</th></tr></thead><tbody>{day_html}</tbody></table></div></section>
<section><h2>Profit trade by trade</h2><div class="box"><svg id="chart" viewBox="0 0 900 260" width="100%"></svg></div></section>
<section><h2>All {a['n']} trades</h2><div class="box scroll"><table id="log"><thead><tr><th>Opened (UTC)</th><th>Market</th><th>Side</th><th class="r">Size</th><th class="r">Entry</th><th class="r">Close</th><th class="r">Held</th><th class="r">Fees</th><th class="r">P&amp;L</th><th>Session</th></tr></thead><tbody></tbody></table></div></section>
<p class="foot">Made by Cashed Risk Review. US session times use New York time (open 9:30, close 16:00). Flags are pointers for a human check, not proof.</p>
</div>
<script>
const T={json.dumps(rows)};
const fm=n=>(n<0?"−$":"+$")+Math.abs(n).toLocaleString("en-GB",{{minimumFractionDigits:2,maximumFractionDigits:2}});
const tb=document.querySelector("#log tbody");
T.forEach(t=>{{const [o,m,s,sz,en,cl,h,f,p,se]=t;const tr=document.createElement("tr");
tr.innerHTML=`<td class="num">${{o}}</td><td>${{m}}</td><td><span class="tag">${{s}}</span></td><td class="r num">$${{Math.round(sz).toLocaleString("en-GB")}}</td><td class="r num">${{en}}</td><td class="r num">${{cl}}</td><td class="r num">${{h==null?"?":Math.round(h)+"s"}}</td><td class="r num">$${{f.toFixed(2)}}</td><td class="r num ${{p>0?"pos":"neg"}}">${{fm(p)}}</td><td>${{se}}</td>`;tb.appendChild(tr);}});
(function(){{const svg=document.getElementById("chart"),W=900,H=260,L=70,R=16,Tp=14,B=24;let c=0;const P=[[0,0]];T.forEach((t,i)=>{{c+=t[8];P.push([i+1,c]);}});
const ys=P.map(p=>p[1]);let lo=Math.min(0,...ys),hi=Math.max(0,...ys);if(hi===lo)hi=lo+1;const pad=(hi-lo)*.08;lo-=pad;hi+=pad;
const x=i=>L+(W-L-R)*i/T.length,y=v=>Tp+(H-Tp-B)*(1-(v-lo)/(hi-lo));const ns="http://www.w3.org/2000/svg";
const el=(n,a)=>{{const e=document.createElementNS(ns,n);for(const k in a)e.setAttribute(k,a[k]);svg.appendChild(e);return e;}};
for(let k=0;k<=4;k++){{const v=lo+(hi-lo)*k/4;el("line",{{x1:L,x2:W-R,y1:y(v),y2:y(v),stroke:"var(--line)"}});const t=el("text",{{x:L-8,y:y(v)+4,"text-anchor":"end"}});t.textContent="$"+Math.round(v).toLocaleString("en-GB");}}
el("line",{{x1:L,x2:W-R,y1:y(0),y2:y(0),stroke:"var(--muted)","stroke-dasharray":"3 3"}});
const d=P.map((p,i)=>(i?"L":"M")+x(p[0]).toFixed(1)+" "+y(p[1]).toFixed(1)).join(" ");
el("path",{{d,fill:"none",stroke:"var(--accent)","stroke-width":"2"}});const l=P[P.length-1];el("circle",{{cx:x(l[0]),cy:y(l[1]),r:4,fill:"var(--accent)"}});}})();
</script></body></html>"""
