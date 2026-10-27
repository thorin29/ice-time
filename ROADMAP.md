# Roadmap

## Now (0.1.1)

- Paste Finnly registration links on the settings page; preview, label, pause, remove.
- Reads every link every 30 minutes; keeps the last good read.
- `GET /api/sessions?days=7` — sessions grouped by section, skater and goalie
  counts combined per time slot, link health and warnings.
- Season-turnover warning when an old season's link runs dry.
- Works on Unraid with no Extra Parameters: takes ownership of `/data`, runs as `PUID:PGID` (99:100).

## Next

- **Follow a schedule index page.** Many rinks list their current registration
  links on one schedule page. Optionally save that page and let ice-time pick
  up the new season's links from it each July, instead of pasting them by hand.
- **Spots-opened notice.** Optional webhook when a full goalie slot opens up.
- **Other booking systems.** Keep the feed format and add readers for other
  registration platforms, chosen by the link that's pasted.

## Not planned

- Signing in, registering or paying — sign-ups stay on the rink's site.
- Crawling or discovering pages on its own beyond an index page you choose.
