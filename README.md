# ice-time

A small self-hosted service that reads the public ice-time schedules you care
about — stick & puck, adult drop-in, parent & child, youth open hockey — from a
rink's Finnly registration pages and serves them as one simple JSON feed.

```
Finnly registration pages ──(every 30 min, one page at a time)──► ice-time ──► GET /api/sessions ──► your dashboard
                                                                      │
                                                                      └─ /data/sources.json  (your links)
                                                                         /data/cache.json    (last good read)
```

- **One file, no dependencies.** `ice_time.py`, Python standard library only.
- **No rink is built in.** You paste the registration links for your rink on
  the settings page; they are saved in `/data`, never in the code or image. The
  same container works for any rink that uses Finnly.
- **Read-only.** It never signs in, books or pays. Sign-ups happen on the rink's
  site — every session in the feed carries a link back to its page.

---

## Setup

### Docker

```sh
docker run -d --name ice-time \
  -p 8080:8080 \
  -v /path/to/ice-time-data:/data \
  -e TIMEZONE=America/New_York \
  -e FEED_TOKEN=change-me \
  ghcr.io/<your-github-user>/ice-time:latest
```

Then open `http://<server>:8080/` and add your links (below).

### Unraid (Add Container)

| Field | Value |
|---|---|
| Name | `ice-time` |
| Repository | `ghcr.io/<your-github-user>/ice-time:latest` |
| Icon URL | raw URL of `ice_time_icon.png` in your copy of this repo |
| WebUI | `http://[IP]:[PORT:8080]/` |
| Port | Container `8080` → Host any free port |
| Path | Container `/data` → Host `/mnt/user/appdata/ice-time` |
| Variable `TIMEZONE` | The rink's time zone, e.g. `America/New_York` |
| Variable `FEED_TOKEN` | A long random string (Kairos uses the same one) |
| Variable `SETTINGS_PASSWORD` | Optional — password for the settings page |

No Extra Parameters are needed. The container takes ownership of its appdata
folder and runs as `99:100` (Unraid's `nobody:users`). If you added
`--user 99:100` for 0.1.0, it still works; you can remove it.

The image is built by GitHub Actions on every push to `main`
(`.github/workflows/build.yml`) and published to GitHub Container Registry
under your repo's name. If the package is private, make it public in the
package settings or `docker login ghcr.io` on the server.

### Settings

| Variable | Default | What it does |
|---|---|---|
| `TIMEZONE` | container's zone | The rink's zone. Finnly times are local wall-clock times; this decides what "now" and "the next 7 days" mean. |
| `FEED_TOKEN` | *(none)* | When set, `/api/sessions` needs `Authorization: Bearer <token>` (or `?token=`). Set it if the port is reachable by anyone else. |
| `SETTINGS_PASSWORD` | *(none)* | When set, the settings page asks for it (any user name). |
| `POLL_MINUTES` | `30` | How often every link is re-read. Minimum 10. |
| `PORT` | `8080` | Port inside the container. |
| `DATA_DIR` | `/data` | Where links and the cache are kept. |
| `PUID` / `PGID` | `99` / `100` | The user and group it runs as after taking ownership of `DATA_DIR`. Match your host's owner of the data folder. Ignored when started with `--user`. |

---

## Adding your rink

1. On the rink's website, open the registration page for a session type —
   the page that lists dates with counts like `0/3`. Its address looks like
   `https://<club>.finnlyconnect.com/registration/activityitemv2/<number>`.
   (Links ending in `/activityitem/<number>` work too.)
2. On ice-time's page, paste it into **Add a schedule** and choose
   **Read this link**. ice-time reads the page and shows the upcoming sessions
   it found.
3. Check the three guesses it made from the activity name, then **Add schedule**:
   - **Section** — Stick & puck, Drop-in, Parent & child, Youth open hockey, or
     your own.
   - **Who it counts** — *Skaters*, *Goalies*, or *Everyone* for sessions with no
     split.
   - **Rink name** — what your calendar events will be named after
     (`Drop-in: <Rink name>`).
4. Skater and goalie sign-ups are **separate pages**: add both, with the same
   section and rink name. ice-time combines them into one row per time slot.

Each link shows its status — `OK · 12 upcoming`, an error, or a warning — and
can be edited, paused (**Include in the feed**), or removed. **Check all now**
re-reads everything immediately.

### Each July

Rinks publish a new set of pages for each season (July 1 – June 30, named like
`26/27`), usually with new numbers in the links. When last season's link runs
out of sessions, the settings page — and the feed's `warnings` — say
*"This link is for the 25/26 season — paste the new season's link."* Add the new
links and remove the old ones.

---

## The feed — `GET /api/sessions`

`?days=7` (1–31) sets how far ahead to look; the window runs from now through
the end of the last day.

```jsonc
{
  "service": "ice-time",
  "version": "0.1.0",
  "generatedAt": "2026-10-26T20:00:00+00:00",   // UTC
  "timezone": "America/New_York",                 // or null = container's zone
  "from": "2026-10-26T15:00:00",                 // local, no offset
  "to": "2026-11-02T00:00:00",
  "sections": [                                  // fixed order, then any others A–Z
    {
      "name": "Drop-in",
      "sessions": [
        {
          "section": "Drop-in",
          "rink": "Sample Arena",
          "start": "2026-10-27T06:00:00",        // local wall-clock time, no offset
          "end": "2026-10-27T07:45:00",
          "minutes": 105,
          "skaters": { "enrolled": 1, "max": 24, "remaining": 23 },
          "goalies": { "enrolled": 0, "max": 3, "remaining": 3 },
          "players": null,                       // set instead for "Everyone" pages
          "links": {
            "skaters": "https://yourclub.finnlyconnect.com/registration/activityitemv2/12344",
            "goalies": "https://yourclub.finnlyconnect.com/registration/activityitemv2/12345"
          }
        }
      ]
    }
  ],
  "sources": [ /* one per link: id, label, section, role, rink, season, url, enabled,
                  ok, lastSuccess, lastError, lastErrorAt, upcomingSessions, warnings */ ],
  "warnings": [ "Sample Arena - Adult Drop In (SKATER) 26/27: The site answered 503." ]
}
```

- A count is `null` when that page isn't set up (for example, a skater page
  without its goalie page).
- Sessions keep their last good read if a check fails; the failure shows in
  `sources[].lastError` and `warnings`.
- Times are local to the rink and carry no offset, because that is how the rink
  publishes them. Read them as wall-clock times in `timezone`.

`GET /healthz` returns `{"ok": true, "version": "…"}` for health checks.

---

## Being a good guest

The registration pages are public and need no sign-in, but they are the rink's
website, not an API, and the site's `robots.txt` asks automated tools not to
crawl. ice-time keeps its footprint to roughly what one person checking the
schedule would make: each configured page is read once per `POLL_MINUTES`
(30 by default, never under 10), one page at a time with a pause between, with
a `User-Agent` that names it. It doesn't follow links or crawl. Whether this
fits a given rink's terms is the operator's call — keep the link list short and
the interval relaxed.

## Privacy

Nothing about your rink, your links or your household is in this repository or
the image. Links and cached schedules live only in the `/data` volume. The test
fixtures use a made-up rink ("Sample Arena") and made-up numbers. If you
contribute, keep it that way: no real links, club names or saved pages in
commits.

## Development

```sh
python -m unittest discover -s tests -v       # tests (fixtures are made up)
DATA_DIR=./data PORT=8080 python ice_time.py  # run locally; ./data is git-ignored
```

See `DECISIONS.md` for why it is built this way and `ROADMAP.md` for what's next.
