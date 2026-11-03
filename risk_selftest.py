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
except Exception:
    out.append(traceback.format_exc())
with open("selftest.txt", "w", encoding="utf-8") as f:
    f.write("Build self-test\n\n" + "\n".join(out) + "\n")
print("\n".join(out))
