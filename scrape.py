#!/usr/bin/env python3
"""
Liner Tracker - daily refresh job (API edition).

For each service + direction it calls MSC's own schedule JSON endpoint
(SearchSailingRoutes) directly - the same call the website makes - and reads
routes, cut-offs and IMO straight from the response. Each vessel is then
reconciled with VesselFinder for live position + last AIS. No headless browser,
so it runs reliably on GitHub's servers (PC can be off).

Writes data.json at repo root; never overwrites with an empty result.
"""
import json, re, sys, time, datetime
import requests

API = "https://www.msc.com/api/feature/tools/SearchSailingRoutes"
DATASOURCE = "{E9CCBD25-6FBA-4C5C-85F6-FC4F9E5A931F}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Port IDs captured from MSC's own search form.
PORT = {"Shanghai": 444, "New York": 79, "Laem Chabang": 36,
        "Houston": 86, "Vung Tau": 1292}

# service key -> config. Each board = (from port, to port, service-name filter)
SERVICES = {
 "EM": {"name": "Empire", "asiaPort": "Shanghai", "usPort": "New York", "svc": "EMPIRE"},
 "AM": {"name": "America", "asiaPort": "Laem Chabang", "usPort": "New York", "svc": "AMERICA"},
 "LS": {"name": "Lone Star Express", "asiaPort": "Vung Tau", "usPort": "Houston", "svc": "LONE STAR"},
}

# ---- date helpers ----------------------------------------------------------
def sd(formatted):
    """'Tue 23rd Jun 2026 16:00' -> '23 Jun 16:00'  (time optional)."""
    if not formatted:
        return "-"
    m = re.search(r"(\d{1,2})\w{2}\s+(\w{3})\s+\d{4}(?:\s+(\d{2}:\d{2}))?", formatted)
    if not m:
        return "-"
    return "{} {}{}".format(int(m.group(1)), m.group(2), " " + m.group(3) if m.group(3) else "")

def sdh(date, hour):
    """'Thu 25th Jun 2026' + '06:00' -> '25 Jun 06:00'."""
    m = re.search(r"(\d{1,2})\w{2}\s+(\w{3})", date or "")
    if not m:
        return "-"
    return "{} {}{}".format(int(m.group(1)), m.group(2), " " + hour if hour else "")

def titlecase(s):
    return " ".join(w.capitalize() for w in (s or "").split())

# ---- MSC schedule API ------------------------------------------------------
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

def make_session():
    """Open the search page first so the API call carries real MSC cookies."""
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    try:
        s.get("https://www.msc.com/en/search-a-schedule", timeout=30)
    except Exception as e:
        print("[session prime] {}".format(e), file=sys.stderr)
    return s

def fetch_board(session, from_port, to_port, svc_filter, from_date):
    body = {"FromDate": from_date, "fromPortId": PORT[from_port],
            "toPortId": PORT[to_port], "language": "en", "dataSourceId": DATASOURCE}
    headers = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest",
               "Accept": "application/json, text/plain, */*",
               "Origin": "https://www.msc.com",
               "Referer": "https://www.msc.com/en/search-a-schedule",
               "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors",
               "Sec-Fetch-Dest": "empty"}
    r = session.post(API, json=body, headers=headers, timeout=40)
    r.raise_for_status()
    data = r.json().get("Data", []) or []
    sailings = []
    for group in data:
        name = (group.get("Key", {}) or {}).get("MaritimeServiceName", "") or ""
        if svc_filter and svc_filter not in name.upper():
            continue
        for rt in group.get("Routes", []) or []:
            sailings.append(parse_route(rt))
    return sailings

def parse_route(rt):
    legs = rt.get("RouteScheduleLegDetails", []) or []
    imo = "-"
    if legs:
        imo = ((legs[0].get("Vessel") or {}).get("VesselImoCode")) or "-"
    # full ordered route with dedup of consecutive identical ports
    route = []
    def add(name, code, eta, etd):
        name = titlecase(name)
        if route and route[-1]["port"] == name:
            if eta and eta != "-":
                route[-1]["eta"] = eta
            if etd and etd not in ("-", ""):
                route[-1]["etd"] = etd
            return
        route.append({"port": name, "code": code or "", "eta": eta or "-", "etd": etd or ""})
    for leg in legs:
        add(leg.get("DeparturePortName"), leg.get("DeparturePortUNCode"),
            "-", sd(leg.get("EstimatedDepartureTimeFormatted")))
        for pc in leg.get("PortCalls", []) or []:
            add(pc.get("PortName"), pc.get("PortCode"),
                sdh(pc.get("EstimatedArrivalDate"), pc.get("EstimatedArrivalHour")),
                sdh(pc.get("EstimatedDepartureDate"), pc.get("EstimatedDepartureHour")))
        add(leg.get("ArrivalPortName"), leg.get("ArrivalPortUNCode"),
            sd(leg.get("EstimatedArrivalTimeFormatted")), "")
    c = rt.get("CutOffs", {}) or {}
    return {
        "vessel": (rt.get("VesselName") or "").upper(),
        "voyage": rt.get("DepartureVoyageNo") or "",
        "imo": imo,
        "dep": sd(rt.get("EstimatedDepartureDateFormatted")),
        "arr": sd(rt.get("EstimatedArrivalDateFormatted")),
        "cutoffs": {
            "CY": sd(c.get("ContainerYardCutOffDate")),
            "Reefer": sd(c.get("ReeferCutOffDate")),
            "DG": sd(c.get("DangerousCargoCutOffDate")),
            "SI": sd(c.get("ShippingInstructionsCutOffDate")),
            "VGM": sd(c.get("VerifiedGrossMassCutOffDate")),
        },
        "route": route,
    }

# ---- VesselFinder AIS ------------------------------------------------------
AREA_COORDS = {
 "east asia": (33, 127), "china coast": (30, 123), "south east asia": (5, 107),
 "south china sea": (12, 114), "us east coast": (37, -73), "north west atlantic ocean": (36, -50),
 "north atlantic": (40, -40), "north america west coast": (36, -150), "mid-pacific": (20, -170),
 "north pacific": (40, -160), "caribbean sea": (15, -74), "caribbean": (15, -74),
 "gulf of mexico": (25, -90), "gulf of thailand": (9, 101), "south africa": (-34, 21),
 "east africa": (2, 48), "indian ocean": (-2, 73), "arabian sea": (15, 65), "red sea": (20, 38),
 "bay of bengal": (14, 90), "mediterranean": (37, 5), "persian gulf": (27, 52),
 "yellow sea": (35, 123), "sea of japan": (40, 135), "panama": (9, -79), "north sea": (56, 3),
}
def coords_for(area):
    a = (area or "").lower()
    for k, v in AREA_COORDS.items():
        if k in a:
            return v
    return (None, None)

def age_to_minutes(p):
    p = (p or "").lower()
    if "min" in p:
        m = re.search(r"(\d+)", p); return int(m.group(1)) if m else 1
    h = re.search(r"(\d+)\s*hour", p); d = re.search(r"(\d+)\s*day", p)
    if d: return int(d.group(1)) * 1440
    if h: return int(h.group(1)) * 60
    return 99999
def status_for(m):
    return "ok" if m < 360 else ("warn" if m <= 1440 else "bad")

def vf_detail(imo):
    try:
        r = requests.get("https://www.vesselfinder.com/vessels/details/" + imo,
                         headers={"User-Agent": UA}, timeout=30)
        h = r.text
        i = h.find("The current position")
        if i < 0:
            return None
        s = re.sub(r"<[^>]+>", " ", h[i:i + 400])
        s = re.sub(r"\s+", " ", s)
        area = re.search(r"is at ([^.]+?) reported", s)
        age = re.search(r"reported (.+?) by AIS", s)
        spd = re.search(r"speed of ([\d.]+) knots", s)
        dest = re.search(r"en route to the port of ([^,]+)", s)
        inport = bool(re.search(r"in port|at anchor", s, re.I))
        return {"area": area.group(1).strip() if area else "",
                "age": age.group(1).strip() if age else "",
                "spd": (spd.group(1) + " kn") if spd else ("in port" if inport else ""),
                "dest": dest.group(1).strip() if dest else ""}
    except Exception as e:
        print("[VF {}] {}".format(imo, e), file=sys.stderr)
        return None

def enrich(sailing, cache):
    imo = sailing["imo"]
    if imo == "-":
        sailing.update(pos="-", ais="-", spd="-", dest="", aisMin=99999, st="bad", src="-", lat=None, lon=None)
        return
    if imo not in cache:
        cache[imo] = vf_detail(imo)
        time.sleep(0.15)
    d = cache[imo]
    if not d:
        sailing.update(pos="-", ais="-", spd="-", dest="", aisMin=99999, st="bad", src="-", lat=None, lon=None)
        return
    m = age_to_minutes(d["age"]); lat, lon = coords_for(d["area"])
    sailing.update(pos=d["area"] or "-", ais=d["age"] or "-", spd=d["spd"] or "-",
                   dest=d["dest"] or "", aisMin=m, st=status_for(m), src="VF", lat=lat, lon=lon)

# ---- main ------------------------------------------------------------------
def main():
    from_date = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    session = make_session()
    cache = {}
    out = {}
    for key, cfg in SERVICES.items():
        try:
            asia = fetch_board(session, cfg["asiaPort"], cfg["usPort"], cfg["svc"], from_date)
        except Exception as e:
            print("[{}] asia-out failed: {}".format(key, e), file=sys.stderr); asia = []
        try:
            us = fetch_board(session, cfg["usPort"], cfg["asiaPort"], cfg["svc"], from_date)
        except Exception as e:
            print("[{}] us-out failed: {}".format(key, e), file=sys.stderr); us = []
        for s in asia + us:
            enrich(s, cache)
        print("[{}] asia-out {}, us-out {}".format(key, len(asia), len(us)), file=sys.stderr)
        out[key] = {"name": cfg["name"], "asiaPort": cfg["asiaPort"], "usPort": cfg["usPort"],
                    "asiaOut": asia, "usOut": us}

    total = sum(len(out[k]["asiaOut"]) + len(out[k]["usOut"]) for k in out)
    if total == 0:
        print("No sailings parsed - keeping existing data.json.", file=sys.stderr)
        return
    data = {"updated": datetime.datetime.utcnow().strftime("%d %b %Y, %H:%M UTC")
            + " — live: MSC schedule API + VesselFinder AIS", "services": out}
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print("Wrote data.json with {} sailings".format(total))

if __name__ == "__main__":
    main()
