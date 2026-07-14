#!/usr/bin/env python3
"""
Liner Tracker - per-service email schedule-change alerts (HTML table format).

Runs in GitHub Actions on every push that updates data.json (the daily PC
refresh), or manually via the Actions tab. Diffs data.json (today) against
data_prev.json (yesterday) and emails each service's product manager ONLY the
changes for the service they manage, as a formatted HTML table, via Gmail SMTP.

Changes are based on ETA (arrival), not ETD. Reschedules are reported only when
the calendar day of a port's ETA changes (same-day hour shifts ignored).

Routing (one email per service that has changes) is configured in SVCS below.

Secrets:  GMAIL_USER, GMAIL_APP_PASSWORD (a 16-char Google App Password)
          NOTIFY_RECIPIENTS  JSON mapping service code -> [name, email(s)]
                             email(s): one address, a comma-separated string,
                             or a JSON list of addresses
Env:
  TEST_SEND=1   send a test email to the EMPIRE manager only
  SVC_FILTER    optional: EM | AM | LS to restrict to one service (else all)
"""
import json, os, re, smtplib, subprocess, sys
from email.mime.text import MIMEText

# Recipients come from the NOTIFY_RECIPIENTS secret (JSON), e.g.
# {"EM": ["Name", "a@x.com, b@y.com"], "AM": ["Name", ["a@x.com"]], ...}
_RECIP = json.loads(os.environ.get("NOTIFY_RECIPIENTS") or "{}")
SVCS = {
    "EM": ("EMPIRE",) + tuple(_RECIP.get("EM") or ("", "")),
    "AM": ("AMERICA",) + tuple(_RECIP.get("AM") or ("", "")),
    "LS": ("LONE STAR",) + tuple(_RECIP.get("LS") or ("", "")),
    "CH": ("CHINOOK",) + tuple(_RECIP.get("CH") or ("", "")),
}
DOY = {"Jan": 0, "Feb": 31, "Mar": 59, "Apr": 90, "May": 120, "Jun": 151,
       "Jul": 181, "Aug": 212, "Sep": 243, "Oct": 273, "Nov": 304, "Dec": 334}

# Authoritative service rotations (must match index.html ROTATION). A vessel that
# skips a canonical port is flagged as a port omission. al = name aliases for
# ports whose UN code may vary in the feed.
ROTATION = {
    "EM": {"asiaOut": [("CNSHA", "Shanghai", None), ("CNNGB", "Ningbo", None), ("KRPUS", "Busan", None)],
           "usOut": [("USNYC", "New York", None), ("USBAL", "Baltimore", None), ("USORF", "Norfolk", None),
                     ("USPEF", "Port Everglades", None), ("PAROD", "Rodman / Panama", ["rodman", "panama", "balboa"])]},
    "AM": {"asiaOut": [("THLCH", "Laem Chabang", None), ("CNYTN", "Yantian", None), ("VNHPH", "Haiphong", None),
                       ("VNVUT", "Vung Tau", None), ("SGSIN", "Singapore", None), ("USNYC", "New York", None)],
           "usOut": [("USBAL", "Baltimore", None), ("USORF", "Norfolk", None)]},
    "LS": {"asiaOut": [("VNVUT", "Vung Tau", None), ("CNYTN", "Yantian", None), ("CNNGB", "Ningbo", None),
                       ("CNSHA", "Shanghai", None), ("KRPUS", "Busan", None)],
           "usOut": [("USHOU", "Houston", None), ("USTPA", "Tampa", None), ("USMIA", "Miami", None),
                     ("BSFPO", "Free Port", ["freeport", "free port", "grand bahama"]), ("SGSIN", "Singapore", None)]},
    "CH": {"asiaOut": [("CNYTN", "Yantian", None), ("CNNGB", "Ningbo", None), ("CNSHA", "Shanghai", None),
                       ("CNTAO", "Qingdao", None), ("KRPUS", "Busan", None)],
           "usOut": [("USSEA", "Seattle", None), ("CAVAN", "Vancouver", None), ("CAPRR", "Prince Rupert", None)]},
}


# Loading-region of each canonical port (AIS area substrings). Used to tell a
# genuine leading omission (vessel not yet in the region) from a port the vessel
# already physically sailed (vessel already in/near the loading region).
OMREG = {
    "CNSHA": ["china coast", "east asia", "yellow sea"], "CNNGB": ["china coast", "east asia"],
    "CNYTN": ["south china sea", "south east asia", "china coast", "east asia"],
    "KRPUS": ["east asia", "china coast", "yellow sea", "sea of japan"],
    "VNVUT": ["south east asia", "south china sea", "gulf of thailand"],
    "VNHPH": ["south east asia", "south china sea"], "THLCH": ["south east asia", "gulf of thailand", "south china sea"],
    "SGSIN": ["south east asia", "south china sea"],
    "USNYC": ["us east coast", "north west atlantic"], "USBAL": ["us east coast"], "USORF": ["us east coast"],
    "USPEF": ["us east coast", "caribbean"], "PAROD": ["caribbean", "gulf of mexico", "central america"],
    "USHOU": ["gulf of mexico"], "USTPA": ["gulf of mexico"], "USMIA": ["us east coast", "caribbean", "gulf of mexico"],
    "BSFPO": ["caribbean", "gulf of mexico", "north west atlantic"],
    "CNTAO": ["east asia", "china coast", "yellow sea"],
    "USSEA": ["north america west coast", "us west coast", "north pacific"],
    "CAVAN": ["north america west coast", "us west coast", "north pacific"],
    "CAPRR": ["north america west coast", "north pacific"],
}


def _omatch(route, code, al):
    for r in route:
        if r.get("code") == code:
            return r
        if al:
            pn = (r.get("port") or "").lower()
            if any(a in pn for a in al):
                return r
    return None


def _region_match(pos, code):
    rs = OMREG.get(code)
    if not rs:
        return False
    a = (pos or "").lower()
    return any(r in a for r in rs)


def omissions_for(s, canon, now_ord):
    """Canonical ports a vessel skips. A port is omitted only if absent AND the
    next scheduled port is still future (strictly later day than the snapshot),
    so already-sailed ports are not falsely flagged. For a LEADING port (before
    the vessel's first scheduled call) the vessel must also NOT already be in the
    loading region of its first call -- otherwise it has physically sailed that
    port (e.g. JAMBOREE past Laem Chabang). 2-stop origin->destination routes are
    skipped (schedule detail not yet published)."""
    route = s.get("route", [])
    if len(route) <= 2:
        return []
    present = [_omatch(route, c, al) for (c, n, al) in canon]
    first_p = next((i for i, p in enumerate(present) if p), -1)
    if first_p < 0:
        return []
    am = s.get("aisMin")
    fresh = isinstance(am, (int, float)) and am <= 4320 and bool(s.get("pos") and s.get("pos") != "-")
    in_load = fresh and _region_match(s.get("pos"), canon[first_p][0])  # vessel RELIABLY in loading region
    disch = route[-1]
    out = []
    for i in range(len(canon)):
        if present[i]:
            continue
        j = i + 1
        while j < len(canon) and not present[j]:
            j += 1
        bound = present[j] if j < len(canon) else disch
        if not bound:
            continue
        bt = bound.get("eta") if (bound.get("eta") and bound.get("eta") != "-") else bound.get("etd")
        bo = doy(bt)
        if bo is None or bo <= now_ord:        # bound not strictly future -> port already passed
            continue
        if i < first_p:                        # LEADING port (before the vessel's first scheduled call)
            if in_load:                        # suppress only with fresh AIS + in region (already sailed); stale AIS (e.g. RANIA) -> flag
                continue
        out.append(canon[i][1])
    return out


def all_omits(data):
    """{service: {(leg, voyage): [skipped ports]}} for the whole board."""
    res = {k: {} for k in SVCS}
    if not data:
        return res
    now_ord = doy(data.get("updated", "")) or 0
    svs = data.get("services", {})
    for svc in SVCS:
        sv = svs.get(svc)
        if not sv:
            continue
        for leg in ("asiaOut", "usOut"):
            canon = ROTATION[svc][leg]
            for s in sv.get(leg, []):
                om = omissions_for(s, canon, now_ord)
                if om:
                    res[svc][(leg, s["voyage"])] = om
    return res


def load_prev():
    try:
        return json.load(open("data_prev.json", encoding="utf-8"))
    except Exception:
        pass
    try:
        out = subprocess.check_output(["git", "show", "HEAD~1:data.json"])
        return json.loads(out)
    except Exception as e:
        print("[notify] no previous data:", e, file=sys.stderr)
        return None


def day_of(s):
    m = re.match(r"\s*(\d{1,2} \w{3})", s or "")
    return m.group(1) if m else ""


def doy(s):
    m = re.search(r"(\d{1,2}) (\w{3})", s or "")
    return DOY[m.group(2)] + int(m.group(1)) if m and m.group(2) in DOY else None


def shift_label(old, new):
    a, b = doy(old), doy(new)
    if a is None or b is None:
        return ""
    d = b - a
    if d > 0:
        return f"+{d}d (delayed)"
    if d < 0:
        return f"{d}d (earlier)"
    return ""


def detect_changes(cur, prev):
    """Return {service_code: [change dict, ...]} for services with changes."""
    out = {k: [] for k in SVCS}
    cs = (cur or {}).get("services", {})
    ps = (prev or {}).get("services", {})
    for svc in SVCS:
        c, p = cs.get(svc), ps.get(svc)
        if not c or not p:
            continue
        for d in ("asiaOut", "usOut"):
            lane = "Asia→US" if d == "asiaOut" else "US→Asia"
            cur_l, prev_l = c.get(d, []), p.get(d, [])
            pb = {s["voyage"]: s for s in prev_l}
            cb = {s["voyage"]: s for s in cur_l}
            for s in cur_l:
                pp = pb.get(s["voyage"])
                base = {"vessel": s["vessel"], "voyage": s["voyage"], "lane": lane}
                if not pp:
                    out[svc].append({**base, "type": "new", "dep": s.get("dep", "")})
                    continue
                if pp.get("vessel") != s.get("vessel"):
                    rotset = set()
                    for dd in ("asiaOut", "usOut"):
                        for cp in ROTATION.get(svc, {}).get(dd, []):
                            rotset.add(cp[0])
                    pfg = [r["port"] for r in pp.get("route", []) if r.get("code") and r["code"] not in rotset and not r["code"].startswith(("US", "PA", "BS"))]
                    old = pp["vessel"]
                    if len(pfg) >= 2:
                        old = "PHASE-OUT " + old + " (reassigned via " + ", ".join(pfg[:4]) + ")"
                    out[svc].append({**base, "type": "swap", "old": old})
                    continue
                pmap = {x["code"]: x for x in pp.get("route", [])}
                ports = []
                for x in s.get("route", []):
                    q = pmap.get(x["code"])
                    if not q:
                        continue
                    ce, pe = x.get("eta"), q.get("eta")
                    if ce and ce != "-" and pe and pe != "-" and day_of(ce) != day_of(pe):
                        ports.append({"port": x["port"], "old": pe, "new": ce,
                                      "shift": shift_label(pe, ce)})
                if ports:
                    out[svc].append({**base, "type": "eta", "ports": ports})
            for s in prev_l:
                if s["voyage"] not in cb:
                    out[svc].append({"vessel": s["vessel"], "voyage": s["voyage"],
                                     "lane": lane, "type": "removed",
                                     "dep": s.get("dep", "")})

    # Port omissions: report a vessel that NEWLY skips a canonical port (vs the
    # previous snapshot). The email shows the vessel's full current omission.
    cur_om, prev_om = all_omits(cur), all_omits(prev)
    for svc in SVCS:
        cs_sv = cs.get(svc) or {}
        for (leg, voy), ports in cur_om.get(svc, {}).items():
            prev_ports = prev_om.get(svc, {}).get((leg, voy), [])
            new_ports = [p for p in ports if p not in prev_ports]
            if not new_ports:
                continue
            vessel = next((s["vessel"] for s in cs_sv.get(leg, []) if s["voyage"] == voy), voy)
            lane = "Asia→US" if leg == "asiaOut" else "US→Asia"
            out[svc].append({"vessel": vessel, "voyage": voy, "lane": lane,
                             "type": "omit", "ports": ports, "new": new_ports})
    return {k: v for k, v in out.items() if v}


# ---- HTML rendering -------------------------------------------------------
NAVY = "#0b3a53"; GREY = "#829ab1"; TEAL = "#0a7d6b"; TEALBG = "#e6fbf6"
AMBER = "#b8860b"; LINE = "#e3e8ee"; BLUE = "#0a66c2"; RED = "#9b2226"
TD = f'style="padding:7px 9px;border-bottom:1px solid {LINE};vertical-align:top;"'
TDV = f'style="padding:7px 9px;border-bottom:1px solid {LINE};vertical-align:top;font-weight:bold;color:{NAVY};"'


def _legcell(lane, rowspan=1):
    rs = f' rowspan="{rowspan}"' if rowspan > 1 else ""
    return f'<td {TD}{rs}><span style="color:{NAVY};font-weight:bold;">{lane}</span></td>'


def html_rows(changes):
    rows = []
    for c in changes:
        v, voy, lane = c["vessel"], c["voyage"], c["lane"]
        if c["type"] == "eta":
            n = len(c["ports"])
            for i, pt in enumerate(c["ports"]):
                vcell = (f'<td {TDV} rowspan="{n}">{v}<div style="font-weight:normal;color:{GREY};font-size:11px;">{voy}</div></td>'
                         + _legcell(lane, n)) if i == 0 else ""
                rows.append(
                    "<tr>" + vcell +
                    f'<td {TD}>{pt["port"]}</td>' +
                    f'<td {TD}><span style="color:{GREY};text-decoration:line-through;">{pt["old"]}</span></td>' +
                    f'<td style="padding:7px 9px;border-bottom:1px solid {LINE};vertical-align:top;background:{TEALBG};color:{TEAL};font-weight:bold;">{pt["new"]}</td>' +
                    f'<td {TD}><span style="color:{AMBER};">{pt["shift"]}</span></td>' +
                    "</tr>")
        elif c["type"] == "new":
            rows.append("<tr style='background:#eef9ff;'>"
                        f'<td {TDV}>{v}<div style="font-weight:normal;color:{GREY};font-size:11px;">{voy}</div></td>'
                        + _legcell(lane) +
                        f'<td {TD} colspan="3" style="padding:7px 9px;border-bottom:1px solid {LINE};color:{BLUE};font-weight:bold;">★ New sailing added</td>'
                        f'<td {TD}>departs {c["dep"]}</td></tr>')
        elif c["type"] == "swap":
            rows.append("<tr style='background:#fff7e6;'>"
                        f'<td {TDV}>{v}<div style="font-weight:normal;color:{GREY};font-size:11px;">{voy}</div></td>'
                        + _legcell(lane) +
                        f'<td {TD} colspan="4" style="padding:7px 9px;border-bottom:1px solid {LINE};color:{AMBER};font-weight:bold;">Vessel swap — replaces {c["old"]}</td></tr>')
        elif c["type"] == "removed":
            rows.append("<tr style='background:#fdeeee;'>"
                        f'<td {TDV}>{v}<div style="font-weight:normal;color:{GREY};font-size:11px;">{voy}</div></td>'
                        + _legcell(lane) +
                        f'<td {TD} colspan="4" style="padding:7px 9px;border-bottom:1px solid {LINE};color:{RED};font-weight:bold;">Removed from board (was {c["dep"]})</td></tr>')
        elif c["type"] == "omit":
            skipped = ", ".join(c["ports"])
            rows.append("<tr style='background:#fdf3e3;'>"
                        f'<td {TDV}>{v}<div style="font-weight:normal;color:{GREY};font-size:11px;">{voy}</div></td>'
                        + _legcell(lane) +
                        f'<td {TD} colspan="4" style="padding:7px 9px;border-bottom:1px solid {LINE};color:#9a5b00;font-weight:bold;">⚠ Port omission — vessel skips {skipped}</td></tr>')
    return "".join(rows)


def build_html(lines):
    head = (f'<tr style="background:{NAVY};color:#fff;">'
            '<th align="left" style="padding:8px 9px;">Vessel</th>'
            '<th align="left" style="padding:8px 9px;">Leg</th>'
            '<th align="left" style="padding:8px 9px;">Port</th>'
            '<th align="left" style="padding:8px 9px;">Previous ETA</th>'
            '<th align="left" style="padding:8px 9px;">New ETA</th>'
            '<th align="left" style="padding:8px 9px;">Shift</th></tr>')
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;color:#102a43;font-size:14px;">'
        '<p>Dear Liner, below are the daily changes in your service:</p>'
        '<table style="border-collapse:collapse;width:100%;max-width:760px;font-size:13px;border:1px solid ' + LINE + ';">'
        '<thead>' + head + '</thead><tbody>' + html_rows(lines) + '</tbody></table></div>')


def main():
    cur = json.load(open("data.json", encoding="utf-8"))
    prev = load_prev()
    changes = detect_changes(cur, prev)

    svc_filter = (os.environ.get("SVC_FILTER") or "").strip().upper()
    if svc_filter in SVCS:
        changes = {k: v for k, v in changes.items() if k == svc_filter}

    test = os.environ.get("TEST_SEND") == "1"
    if test:
        changes = {"EM": [{"vessel": "TEST", "voyage": "—", "lane": "Asia→US",
                           "type": "eta", "ports": [{"port": "(test)",
                           "old": "—", "new": "pipeline OK", "shift": ""}]}]}

    if not changes:
        print("[notify] no changes; no email sent.")
        return

    # SMTP config — provider-agnostic. Defaults keep old Gmail behaviour;
    # set SMTP_* secrets to use a transactional provider (e.g. Brevo).
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER") or os.environ.get("GMAIL_USER")
    pw = os.environ.get("SMTP_PASS") or os.environ.get("GMAIL_APP_PASSWORD")
    mail_from = os.environ.get("MAIL_FROM") or user
    if not user or not pw:
        total = sum(len(v) for v in changes.values())
        print(f"[notify] SMTP secrets not set — skipping. {total} change group(s) "
              f"across {len(changes)} service(s) would have been emailed.")
        return

    date = (cur.get("updated", "").split(",")[0] or "").strip()
    sent = 0
    srv = smtplib.SMTP_SSL(host, port) if port == 465 else smtplib.SMTP(host, port)
    try:
        if port != 465:
            srv.starttls()
        srv.login(user, pw)
        for svc, lines in changes.items():
            label, _name, to = SVCS[svc]
            if not to:
                print(f"[notify] no recipient configured for {label} - skipped")
                continue
            tos = to if isinstance(to, list) else [x.strip() for x in str(to).split(",") if x.strip()]
            msg = MIMEText(build_html(lines), "html")
            msg["Subject"] = f"{label} - MSC Schedule daily change - {date}"
            msg["From"] = mail_from
            msg["To"] = ", ".join(tos)
            srv.sendmail(mail_from, tos, msg.as_string())
            sent += 1
            print(f"[notify] emailed {len(lines)} {label} change group(s) to {', '.join(tos)}")
    finally:
        try:
            srv.quit()
        except Exception:
            pass
    print(f"[notify] done — {sent} service email(s) sent via {host}.")


if __name__ == "__main__":
    main()
