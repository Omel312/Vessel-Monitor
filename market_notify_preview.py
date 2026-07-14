#!/usr/bin/env python3
# PREVIEW ONLY - sends a sample of the proposed daily missing-capacity email
# to the address in the MARKET_PREVIEW_RECIPIENT secret for review.
# This does NOT change production recipients in market_notify.py.
# Triggered manually via the "Market Notify Preview" workflow (workflow_dispatch).
import os
import market_notify as mn

NY = {"new york", "newark", "new york / newark"}
SAV = {"savannah"}
HOU = {"houston"}
VT = ["Vung Tau", "Cai Mep"]

# Proposed lanes - daily missing-capacity (blank sailing)
# tables, one per lane, ALL carriers including ZIM+MSC.
LANES = [
    ("Shanghai", NY, "New York / Newark"),
    ("Shanghai", SAV, "Savannah"),
    ("Shanghai", HOU, "Houston"),
    ("Ningbo", NY, "New York / Newark"),
    ("Ningbo", SAV, "Savannah"),
    ("Ningbo", HOU, "Houston"),
    ("Yantian", NY, "New York / Newark"),
    ("Yantian", SAV, "Savannah"),
    ("Yantian", HOU, "Houston"),
    (VT, NY, "New York / Newark"),
    (VT, SAV, "Savannah"),
    (VT, HOU, "Houston"),
]


def as_list(a):
    return a if isinstance(a, list) else [a]


def get_cells(s, asia):
    """Merge cells across one or more Asia load ports, de-duping shared sailings."""
    seen = set()
    out = []
    for p in as_list(asia):
        for c in (s.get("ports") or {}).get(p, []) or []:
            k = (c.get("vessel"), c.get("week"), c.get("etd"))
            if k in seen:
                continue
            seen.add(k)
            out.append(c)
    return out


def lane_services(mj, asia, us_targets, exclude):
    out = []
    alls = mj.get("alliances") or {}
    order = mn.ALORDER + [a for a in alls if a not in mn.ALORDER]
    for al in order:
        if al in exclude:
            continue
        obj = alls.get(al)
        if not obj:
            continue
        for s in obj.get("services", []):
            if get_cells(s, asia) and mn.calls_us(s, us_targets):
                out.append((al, s))
    return out


def gaps_html(mj, asia, us_targets, exclude):
    weeks = mj.get("weeks", [])
    teu_map = mj.get("vesselTeu", {})
    rows = []
    total_missing = 0
    for al, s in lane_services(mj, asia, us_targets, exclude):
        nm = mn.svc_name(s)
        cells = get_cells(s, asia)
        byw = {}
        for c in cells:
            byw.setdefault(c.get("week"), []).append(c)
        caps = []
        for c in cells:
            try:
                n = int(teu_map.get(c.get("vessel")))
                if n > 0:
                    caps.append(n)
            except Exception:
                pass
        popw = sorted(byw.keys())
        if not popw:
            continue
        avg = round(sum(caps) / len(caps)) if caps else 0
        interior = [w for w in weeks if popw[0] < w < popw[-1] and w not in byw]
        if interior and avg:
            miss = avg * len(interior)
            total_missing += miss
            rows.append((al, nm, interior, avg, miss))
    if not rows:
        return ('<p style="font:13px Arial;color:#2c6ca0">No capacity gaps: every '
                'service has a sailing in each interior week of the window.</p>')
    h = ('<table cellpadding="6" cellspacing="0" style="border-collapse:collapse;'
         'font:12px Arial;border:1px solid #ccc">')
    h += ('<tr style="background:#7a1f1f;color:#fff"><th align="left">Alliance</th>'
          '<th align="left">Service</th><th>Blank week(s)</th>'
          '<th align="right">Avg capacity / sailing</th>'
          '<th align="right">Missing capacity</th></tr>')
    for al, nm, interior, avg, miss in rows:
        color = mn.ALCOLOR.get(al, "#555")
        wk = ", ".join("W%s" % w for w in interior)
        h += '<tr style="border-top:1px solid #eee">'
        h += ('<td><span style="background:%s;color:#fff;padding:1px 5px;'
              'border-radius:3px;font-size:10px">%s</span></td>') % (color, mn.esc(al))
        h += ('<td>%s</td><td align="center">%s</td><td align="right">%s TEU</td>'
              '<td align="right"><b style="color:#b3261e">%s TEU</b></td></tr>') % (
            mn.esc(nm), wk, "{:,}".format(avg), "{:,}".format(miss))
    h += ('<tr style="border-top:2px solid #999;background:#faf0f0">'
          '<td colspan="4" align="right"><b>Total missing capacity</b></td>'
          '<td align="right"><b style="color:#b3261e">%s TEU</b></td></tr>') % (
        "{:,}".format(total_missing))
    return h + "</table>"


def lane_block(mj, asia, us_targets, label):
    asia_label = " / ".join(as_list(asia))
    p = []
    p.append('<h3 style="font:bold 16px Arial;color:#13324a;margin:24px 0 4px">'
             '%s &rarr; %s</h3>' % (mn.esc(asia_label), mn.esc(label)))
    p.append('<h4 style="font:bold 13px Arial;color:#7a1f1f;margin:14px 0 6px">'
             'Capacity gaps &mdash; missing sailings</h4>')
    p.append(gaps_html(mj, asia, us_targets, []))
    p.append('<div style="font:11px Arial;color:#888;margin:4px 0 0">Gap = a blank '
             'week between the first and last scheduled sailing of a service from %s '
             '(a dropped weekly sailing). The newest week and any phase-in weeks at '
             'the window start are excluded as not-yet-published. Missing capacity = '
             'the service average vessel capacity across its scheduled sailings.'
             '</div>' % mn.esc(asia_label))
    return "".join(p)


def build_html(mj):
    p = []
    p.append('<div style="font:14px Arial;color:#111">Dear Liner, below are the '
             'weekly missing-capacity (blank sailing) tables for your monitored '
             'lanes, covering all carriers including ZIM+MSC.</div>')
    for asia, us, label in LANES:
        p.append(lane_block(mj, asia, us, label))
    p.append('<div style="font:11px Arial;color:#888;margin-top:20px">Auto-generated '
             'from Liner Tracker market profile. Updated %s.</div>' % mn.esc(
                 (mj or {}).get("updated", "")))
    return '<div style="overflow-x:auto">' + "".join(p) + "</div>"


def main():
    to = os.environ.get("MARKET_PREVIEW_RECIPIENT")
    if not to:
        print("[preview] MARKET_PREVIEW_RECIPIENT secret not set - nothing sent")
        return
    mj = mn.load("market.json")
    html = build_html(mj)
    subject = "[PREVIEW] Market missing-capacity by lane - proposed daily"
    mn.send(to, subject, html)


if __name__ == "__main__":
    main()
