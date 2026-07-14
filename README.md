# Liner Tracker

A self-updating dashboard for MSC's Asia ⇄ North America gateway services, plus a weekly competitor **Market Profile** across the major alliances. The page is static (served by GitHub Pages); scheduled jobs refresh the data behind it and email daily change alerts to the relevant desks.

**Live site:** https://omel312.github.io/liner-tracker/

## What it shows

### 1. Schedule Tracker (top)

Live line-up for four MSC services — **Empire, America, Lone Star, Chinook** — in both directions (Asia → US / Canada and back). For each vessel:

- full port-by-port rotation with ETA / ETD and the CY cut-off at every load port;
- current position + last AIS signal (VesselFinder) on a map;
- automatic **port-omission badges** when a vessel skips a canonical port call;
- a **Port omissions & changes** panel diffing today vs. yesterday (new / removed sailings, vessel swaps, ETA shifts, new omissions);
- a **Port watch — congestion** panel derived from AIS dwell + schedule drift.

### 2. Market Profile (bottom)

A weekly (ISO-week) grid of direct competitor sailings by alliance — **Gemini, Ocean Alliance, Premier Alliance, ZIM+MSC**. Choose an alliance, an Asia load port, and optionally a US port; each cell shows the vessel, its ETA at that Asia port, the CY gate-in, and vessel capacity (TEU), plus transit times. A **Market changes** panel tracks day-over-day differences.

## Services (all MSC-operated)

| Service | Asia load ports | North America ports |
| --- | --- | --- |
| **Empire** | Shanghai, Ningbo, Busan | New York, Baltimore, Norfolk, Port Everglades |
| **America** | Laem Chabang, Yantian, Haiphong, Vung Tau, Singapore | New York, Baltimore, Norfolk |
| **Lone Star** | Vung Tau, Yantian, Ningbo, Shanghai, Busan | Houston, Mobile, Tampa, Miami |
| **Chinook** | Yantian, Ningbo, Shanghai, Qingdao, Busan | Seattle, Vancouver, Prince Rupert |

## Repository layout

```
index.html          the dashboard (reads the JSON files below)
data.json           schedule tracker — today
data_prev.json      schedule tracker — yesterday (powers the change panel)
market.json         market profile — today
market_prev.json    market profile — yesterday (powers the change panel)
notify.py           per-service schedule-change email alerts
market_notify.py    daily market-profile email (capacity gaps + snapshot)
.github/workflows/  GitHub Actions (email jobs + Pages deploy)
scrape.py           legacy Playwright fetch (superseded — see notes)
```

Only two generations of each dataset are kept — today and yesterday. The _prev files exist solely to compute day-over-day changes, so nothing older accumulates.

## Data pipelines

Both datasets are refreshed by scheduled jobs that run through a **local Chrome** browser on the maintainer's PC. This is deliberate: MSC and VesselFinder return HTTP 403 to datacenter / cloud IPs, so the scrape needs a residential IP and cannot run on GitHub's shared runners.

- **Schedule refresh (daily ~04:00 HK)** — pulls the four MSC services from MSC's internal SearchSailingRoutes API (full rotations + per-port CY cut-offs) and positions from VesselFinder, rolls data_prev.json, and commits data.json.
- **Market refresh (daily ~05:00 HK)** — pulls competitor line-ups from each carrier's own site (Ocean Alliance via CMA CGM routing-finder, Gemini via the Maersk schedules API, Premier via ONE Route Finder) and re-derives the ZIM+MSC rows from data.json. Caches vessel TEU, computes transit, rolls market_prev.json, and commits market.json.

## Email alerts

Two GitHub Actions send HTML emails via SMTP (credentials held as repo secrets).

**Schedule changes** (notify.py) — on every data.json update, each service manager receives only their service's daily changes (new / removed sailings, vessel swaps, ETA reschedules, port omissions). Recipients are configured per service in `notify.py`.

**Market snapshot** (market_notify.py) — a daily email per desk with a capacity-gaps table (weeks with no sailing, valued at the service's average vessel capacity) and one market-profile snapshot per lane. Recipient lanes and scopes are configured in `market_notify.py`.

Change detection ignores the week that rolls off the bottom (and the new week added at the top) when the 8-week window advances, so a shifting window is never mis-reported as a change.

## How it is served

GitHub Pages serves index.html, which fetches the JSON files at load time — no server to run. The scheduled jobs commit fresh data; Pages redeploys within ~1–2 minutes.

## Operating notes

- **Residential IP required.** The refresh jobs run through the local Chrome extension and will not work from cloud runners (MSC / VesselFinder block those). If Chrome is not connected, a run leaves the last-good data in place instead of publishing empties.
- **Positions are area-level.** VesselFinder's free tier gives ocean-basin accuracy, so the map is schematic, not exact GPS.
- **scrape.py is legacy.** The original Playwright scraper (and its update.yml cron) is superseded by the browser-based refresh jobs above; the file is kept for reference.
- **Data is never invented.** When a carrier cannot be fetched, the affected service keeps its previous values; TEU stays N/A until a real figure is cached.
