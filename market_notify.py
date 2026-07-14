#!/usr/bin/env python3
import os, json, smtplib, ssl, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

ALORDER = ["ZIM+MSC","Gemini","Ocean Alliance","Premier Alliance"]
ALCOLOR = {"ZIM+MSC":"#0b3d5c","Gemini":"#1f6fb2","Ocean Alliance":"#2e7d4f","Premier Alliance":"#7a4ea3"}

# One entry per recipient. Each lane: asia load port, set of US target ports, label, and alliances to exclude.
# Recipient emails come from the MARKET_RECIPIENTS secret: a JSON array with one
# address per CONFIGS entry, in the same order.
RECIPIENTS = json.loads(os.environ.get("MARKET_RECIPIENTS") or "[]")
NY = {"new york","newark","new york / newark"}
CONFIGS = [
    {"title": "Shanghai to New York / Newark",
     "lanes": [{"asia": "Shanghai", "us": NY, "us_label": "New York / Newark", "exclude": []}]},
    {"title": "Yantian to New York / Newark",
     "lanes": [{"asia": "Yantian", "us": NY, "us_label": "New York / Newark", "exclude": ["ZIM+MSC"]}]},
    {"title": "Shanghai and Yantian to Houston",
     "lanes": [{"asia": "Shanghai", "us": {"houston"}, "us_label": "Houston", "exclude": ["ZIM+MSC"]},
               {"asia": "Yantian", "us": {"houston"}, "us_label": "Houston", "exclude": ["ZIM+MSC"]}]},
]

def load(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def esc(s):
    return (str(s if s is not None else "")).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def fmt_teu(t):
    try:
        n = int(t)
        return "{:,}".format(n) if n > 0 else ""
    except Exception:
        return ""

def svc_name(s):
    return s.get("svc") or s.get("name") or "?"

def calls_us(svc, us_targets):
    for p in (svc.get("us") or []):
        if str(p).strip().lower() in us_targets:
            return True
    return False

def lane_services(mj, asia, us_targets, exclude):
    out = []
    alls = mj.get("alliances") or {}
    order = ALORDER + [a for a in alls if a not in ALORDER]
    for al in order:
        if al in exclude:
            continue
        obj = alls.get(al)
        if not obj:
            continue
        for s in obj.get("services", []):
            cells = (s.get("ports") or {}).get(asia) or []
            if cells and calls_us(s, us_targets):
                out.append((al, s))
    return out

def grid_html(mj, asia, us_targets, exclude):
    weeks = mj.get("weeks", [])
    teu_map = mj.get("vesselTeu", {})
    svcs = lane_services(mj, asia, us_targets, exclude)
    if not svcs:
        return '<p style="font:12px Arial;color:#888">No services on this lane.</p>'
    h = '<table cellpadding="5" cellspacing="0" style="border-collapse:collapse;font:11px Arial;border:1px solid #bbb">'
    h += '<tr style="background:#13324a;color:#fff"><th align="left" style="min-width:190px">Service (weekly TEU)</th>'
    for w in weeks:
        h += '<th style="border:1px solid #24506e;min-width:118px">W%s</th>' % w
    h += '</tr>'
    for al, s in svcs:
        nm = svc_name(s); color = ALCOLOR.get(al, "#555")
        teu = fmt_teu(s.get("teu"))
        us = " &middot; ".join(s.get("us") or [])
        tr = (s.get("transit") or {}).get(asia, {})
        trstr = " &middot; ".join("%s %sd" % (esc(k), esc(v)) for k, v in tr.items())
        hd = ('<div><span style="background:%s;color:#fff;padding:1px 5px;border-radius:3px;font-size:9px">%s</span> <b>%s</b></div>'
              '<div style="color:#666;font-size:10px">%s &middot; US: %s</div>'
              '<div style="color:#666;font-size:10px">Asia ETA @ <b>%s</b></div>'
              '<div style="color:#666;font-size:10px">Transit from %s: %s</div>') % (
              color, esc(al), esc(nm), ("%s TEU/wk" % teu if teu else "TEU n/a"), esc(us), esc(asia), esc(asia), (trstr or "&mdash;"))
        row = '<tr style="vertical-align:top"><td style="border:1px solid #bbb;background:#f6f8fa">%s</td>' % hd
        byw = {}
        for c in (s.get("ports") or {}).get(asia, []):
            byw.setdefault(c.get("week"), []).append(c)
        for w in weeks:
            inner = ""
            for c in byw.get(w, []):
                t = fmt_teu(teu_map.get(c.get("vessel")))
                inner += ('<div style="margin-bottom:5px;padding-bottom:4px;border-bottom:1px dotted #ddd">'
                          '<b>%s</b><br><span style="color:#c0392b">ETA %s</span><br>'
                          '<span style="color:#2c6ca0">CY %s</span>%s</div>') % (
                          esc(c.get("vessel")), esc(c.get("etd")), esc(c.get("cy") or "-"),
                          ('<br><span style="color:#2c6ca0">%s TEU</span>' % t if t else ""))
            row += '<td style="border:1px solid #bbb">%s</td>' % (inner or '<span style="color:#ccc">&middot;</span>')
        h += row + '</tr>'
    return h + '</table>'

def gaps_html(mj, asia, us_targets, exclude):
    weeks = mj.get("weeks", [])
    teu_map = mj.get("vesselTeu", {})
    rows = []
    total_missing = 0
    for al, s in lane_services(mj, asia, us_targets, exclude):
        nm = svc_name(s)
        cells = (s.get("ports") or {}).get(asia, [])
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
        return '<p style="font:13px Arial;color:#2c6ca0">No capacity gaps: every service has a sailing in each interior week of the window.</p>'
    h = '<table cellpadding="6" cellspacing="0" style="border-collapse:collapse;font:12px Arial;border:1px solid #ccc">'
    h += '<tr style="background:#7a1f1f;color:#fff"><th align="left">Alliance</th><th align="left">Service</th><th>Blank week(s)</th><th align="right">Avg capacity / sailing</th><th align="right">Missing capacity</th></tr>'
    for al, nm, interior, avg, miss in rows:
        color = ALCOLOR.get(al, "#555")
        wk = ", ".join("W%s" % w for w in interior)
        h += '<tr style="border-top:1px solid #eee">'
        h += '<td><span style="background:%s;color:#fff;padding:1px 5px;border-radius:3px;font-size:10px">%s</span></td>' % (color, esc(al))
        h += '<td>%s</td><td align="center">%s</td><td align="right">%s TEU</td><td align="right"><b style="color:#b3261e">%s TEU</b></td></tr>' % (esc(nm), wk, "{:,}".format(avg), "{:,}".format(miss))
    h += '<tr style="border-top:2px solid #999;background:#faf0f0"><td colspan="4" align="right"><b>Total missing capacity</b></td><td align="right"><b style="color:#b3261e">%s TEU</b></td></tr>' % "{:,}".format(total_missing)
    return h + '</table>'

def lane_block(mj, lane):
    asia = lane["asia"]; ust = lane["us"]; exc = lane.get("exclude", []); lab = lane["us_label"]
    tag = ' <span style="font:400 12px Arial;color:#888">(competitors only)</span>' if exc else ''
    p = []
    p.append('<h3 style="font:bold 16px Arial;color:#13324a;margin:24px 0 4px">%s &rarr; %s%s</h3>' % (esc(asia), esc(lab), tag))
    p.append('<h4 style="font:bold 13px Arial;color:#7a1f1f;margin:14px 0 6px">Capacity gaps &mdash; missing sailings</h4>')
    p.append(gaps_html(mj, asia, ust, exc))
    p.append('<div style="font:11px Arial;color:#888;margin:4px 0 0">Gap = a blank week between the first and last scheduled %s sailing of a service (a dropped weekly sailing). The newest week and any phase-in weeks at the window start are excluded as not-yet-published. Missing capacity = the service average vessel capacity across its scheduled sailings.</div>' % esc(asia))
    p.append('<h4 style="font:bold 13px Arial;color:#13324a;margin:16px 0 6px">Market Profile snapshot</h4>')
    p.append(grid_html(mj, asia, ust, exc))
    return "".join(p)

def build_html(mj, cfg):
    p = []
    p.append('<div style="font:14px Arial;color:#111">Dear Liner, below is the market profile of %s, including the weekly capacity gaps.</div>' % esc(cfg["title"]))
    for lane in cfg["lanes"]:
        p.append(lane_block(mj, lane))
    p.append('<div style="font:11px Arial;color:#888;margin-top:20px">Auto-generated from Liner Tracker market profile. Updated %s.</div>' % esc((mj or {}).get("updated", "")))
    return '<div style="overflow-x:auto">' + "".join(p) + '</div>'

def send(to_addr, subject, html):
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    user = os.getenv("SMTP_USER") or os.getenv("GMAIL_USER")
    pw = os.getenv("SMTP_PASS") or os.getenv("GMAIL_APP_PASSWORD")
    frm = os.getenv("MAIL_FROM") or user
    if not (user and pw):
        print("No SMTP creds; skipping send to", to_addr)
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = frm; msg["To"] = to_addr
    msg.attach(MIMEText("See the HTML version of this message.", "plain"))
    msg.attach(MIMEText(html, "html"))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx) as srv:
            srv.login(user, pw); srv.sendmail(frm, [to_addr], msg.as_string())
    else:
        with smtplib.SMTP(host, port) as srv:
            srv.starttls(context=ctx); srv.login(user, pw); srv.sendmail(frm, [to_addr], msg.as_string())
    print("Sent to", to_addr)
    return True

def main():
    mj = load("market.json")
    if not RECIPIENTS:
        print("[market_notify] MARKET_RECIPIENTS secret not set - nothing sent")
        return
    for cfg, to in zip(CONFIGS, RECIPIENTS):
        html = build_html(mj, cfg)
        subject = "Market Profile — " + cfg["title"]
        send(to, subject, html)

if __name__ == "__main__":
    main()
