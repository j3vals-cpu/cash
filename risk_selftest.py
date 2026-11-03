"""Quick check used by the build: parse one trade row and build a report. Writes the result to selftest.txt."""
import traceback

out = []
try:
    import risk_review
    import risk_report
    row = ("KORU-USD Perpetual\nabc\nSHORT\tSETTLED\t$15,726\t5×\t11.843\t11.834\t— / —\t$6.29\t$5.66\t"
           "02/11/2026, 14:08:43\t02/11/2026, 14:09:16")
    t = risk_review.parse_export(row)
    out.append(f"parsed trades: {len(t)}")
    h = risk_report.build_html(t)
    out.append(f"report ok: {'trade overview' in h} ({len(h)} chars)")
    import risk_scan
    api = [{"market": "BTC-USD", "side": sd, "size_usd": 100000, "pnl": p, "opened_at": o, "closed_at": c}
           for sd, p, o, c in [("LONG", 500, "2026-11-01T10:00:00Z", "2026-11-01T11:00:00Z")]]
    api2 = [{"market": "BTC-USD", "side": "SHORT", "size_usd": 95000, "pnl": -480,
             "opened_at": "2026-11-01T10:00:20Z", "closed_at": "2026-11-01T10:59:00Z"}]
    res = risk_scan.scan([{"account": {"id": "A", "username": "bob"}, "trades": api},
                          {"account": {"id": "B", "username": "bob"}, "trades": api2}])
    out.append(f"scan same-person pairs: {len(res['same'])} (expect 1)")
    out.append(f"scan report ok: {'All-users risk scan' in risk_scan.build_scan_html(res, risk_report.CSS)}")
except Exception:
    out.append(traceback.format_exc())
try:
    import risk_app
    app = risk_app.App()
    app.update()
    app.open_settings()
    app.update()
    out.append("window opens: True")
    app.destroy()
except Exception:
    out.append("window: " + traceback.format_exc())
with open("selftest.txt", "w", encoding="utf-8") as f:
    f.write("Build self-test\n\n" + "\n".join(out) + "\n")
print("\n".join(out))
