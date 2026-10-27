# Decisions

Why ice-time is built the way it is. Newest last.

## 0.1.0

**A separate container, not part of the dashboard.** Everything specific to one
rink — its links, its activity names, its seasons — stays in this service's
`/data` volume. The dashboard only knows "there is a feed at this address". It
keeps rink data out of the dashboard's database and code, and lets anyone point
the same image at a different rink.

**Configured by pasting links, not by editing a file.** The settings page takes
a registration link, reads it on the spot, shows what it found, and pre-fills
the section, skater/goalie and rink name from the activity's name. People
recognise the page they sign up on; they don't need to know ids or JSON.

**Read the data the page already carries.** Finnly registration pages are
server-rendered and embed the full schedule as JSON
(`window.activityV2Boot … registrationScheduleList`), with start, end,
enrolled, max and remaining for every session. There is no separate API call to
reproduce and no sign-in. The parser finds that array with a small JS-aware
bracket matcher (it skips over strings, so `]` inside a description can't cut
it short) and raises a clear error if the page changes shape.

**Only Finnly registration links are accepted.** The settings page takes a URL
from whoever can open it, so the server refuses anything that isn't
`https://<club>.finnlyconnect.com/registration/activityitem[v2]/<number>`. That
stops it from being used to fetch other addresses on your network. Older
`activityitem/` links are rewritten to the `activityitemv2/` page, which is the
one that carries the schedule.

**Skater and goalie pages combine by start time.** Rinks publish them as
separate activities with identical start times. Each link is tagged with a
section, rink and role; the feed merges rows that share all of section, rink
and start, so a drop-in slot shows `skaters` and `goalies` together.

**Polite polling.** One page at a time, a pause between pages, every 30 minutes
by default and never more often than every 10, with a `User-Agent` that names
the service. The site's robots.txt discourages bots; this keeps the load at
about what one person refreshing the page would make. See README.

**Last good read wins.** A failed check keeps the previous sessions and records
the error on that link, so a brief outage doesn't empty the dashboard.

**Season turnover is surfaced, not guessed.** Seasons run July 1 – June 30 and
links usually change with them. ice-time doesn't try to discover new links; when
an old-season link has no upcoming sessions it says so in plain words on the
settings page and in the feed's `warnings`.

**Times stay local and unzoned.** Finnly publishes wall-clock times without an
offset. The feed passes them through as-is and names the zone (`TIMEZONE`)
rather than converting, so there's no daylight-saving guesswork in two places.

**Standard library only, one file.** No dependencies to update or audit; the
image is `python:3.12-slim` plus one file, running as a non-root user.

**Optional protection.** `FEED_TOKEN` (Bearer or `?token=`) for the feed,
`SETTINGS_PASSWORD` (Basic auth) for the settings page, and settings forms
refuse posts from other sites. Both are off by default for a LAN-only setup;
turn them on if the port is reachable from elsewhere.

**No real data in the repo.** Tests use a made-up "Sample Arena" fixture.
Real links live only in `/data`, which is git-ignored for local runs.

## 0.1.1

**Writable data folder with no extra settings.** 0.1.0 ran as a fixed user
(10001), which can't write an Unraid appdata folder owned by `nobody:users`
(99:100) — it needed `--user 99:100`. Now the service starts as root only to
take ownership of `DATA_DIR`, then permanently drops to `PUID:PGID` (default
99:100) before reading any page or opening the port to requests. Done in Python
(`drop_privileges`) rather than an entrypoint script, so the image stays one
file. Started with `--user`, it never runs as root at all.

