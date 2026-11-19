# Permit Alerts

Watches Recreation.gov for river permits, wilderness permits, and campsites
that open up, and alerts your whole group on Discord and by email within
about a minute. Every opening is logged, so over time you can see when spots
tend to appear.

- **Searches:** pick one or more permits/campgrounds, a date window, and a group. Run as many searches at once as you like.
- **Groups:** each group gets its own private Discord channel that the app creates and manages.
- **Alerts:** you get one alert when something opens, one reminder an hour later if it's still open, and then nothing more. If it closes and opens again, that counts as a new opening.
- **Trends:** charts of when openings appear (time of day, day of week), how long they last, and which trip dates they're for.

It runs on your own computer (Windows today) and is built so it can move to
a server later without changes (see [Running on a server](#running-on-a-server)).

## What it can watch

| Type | Examples tested | How it's checked |
|---|---|---|
| River permits (most) | Ruby Horsethief, Dinosaur Green & Yampa (Lodore, Deerlodge), Middle Fork & Main Salmon, Selway, Desolation Gray | `permits` availability API |
| Newer river permits | Westwater, Canyonlands (Cataract), Dolores | `permitwbe` availability API |
| Wilderness / trailhead quotas | Mt. Whitney, Yosemite wilderness | `permitinyo` availability API |
| Campgrounds | Watchman (Zion), Jumbo Rocks (Joshua Tree), Furnace Creek, Chisos Basin, Upper Pines | campground month API |

The right API is detected automatically. A few permit types (for example
Grand Canyon backcountry) use a different system that isn't supported yet.
For those, the search shows an error instead of failing silently.

## Install on Windows

1. Download this repository (green **Code** button → **Download ZIP**) and unzip it.
2. Open the `windows` folder and double-click **install.bat**.
   - It installs Python if needed, sets up the app, and adds a **Permit Alerts** shortcut to your Desktop and Start menu.
   - It asks whether to start automatically when you sign in. Say yes if this computer stays on.
3. The dashboard opens at <http://localhost:8645>. Keep the Permit Alerts window open (minimizing it is fine). Closing it stops the alerts.

To update later, download the new version and run `install.bat` again. Your searches and history are kept in `%APPDATA%\permit-alerts`.

## Set up Discord (about 5 minutes, once)

The app runs a Discord bot that creates and manages the alert channels for you.

1. **Make a server** for alerts in Discord (the **+** button → *Create My Own*), or use one you own.
2. Go to <https://discord.com/developers/applications> → **New Application** → name it "Permit Alerts".
3. Open **Bot** in the left menu:
   - Click **Reset Token**, then copy the token. You'll paste it into the app.
   - Under *Privileged Gateway Intents*, turn on **Server Members Intent** and save.
4. Open **OAuth2 → URL Generator**: tick **bot**, then under permissions tick **Administrator**. Open the generated link and add the bot to your server.
5. In Discord, turn on **Settings → Advanced → Developer Mode**, then right-click your server icon → **Copy Server ID**.
6. In the dashboard, open **Settings**, paste the **bot token** and **server ID**, save, and restart Permit Alerts.

From then on, each group gets a private channel under a **Permit Alerts**
category. Add people by their Discord username in the group's member list.
If someone isn't in your server yet, the Groups page has an invite link to
send them. Save the group again after they join, and they get access.

## Set up email (optional)

With Gmail: turn on 2-step verification, then create an **app password** at
<https://myaccount.google.com/apppasswords>. In **Settings**, use:

- SMTP host `smtp.gmail.com`, port `587`
- username = your Gmail address, password = the app password
- "Send alerts from" = your Gmail address

Group members' emails are sent as BCC, so members don't see each other's addresses.

## Using it

- **New search:** type a name such as "Westwater" or "Lodore" in the search box, add one or more results, pick your dates and a group, then press **Start search**.
  - *Only these IDs* limits a permit to certain launch points or trailheads (their IDs are listed under the box), or a campground to certain campsite IDs.
  - *Group size* skips campsites that are too small, and trailhead quotas without enough spots for everyone. River permits count launches, not people, so group size doesn't filter them.
  - *Minimum nights* (campgrounds) only alerts for that many nights in a row at one site.
  - *Check every* defaults to 45 seconds. The lowest allowed is 15 seconds. Checking faster is more likely to get you rate-limited by Recreation.gov.
- **When a search starts**, it sends one "already open" summary of anything open right now (with no reminder). After that, it only alerts on new openings.
- **Send test alert** on the Groups page checks that Discord and email work.

### Good to know

- This uses the same public data the Recreation.gov website shows. It is not an official API, so it can change. If a search shows an error, check the dashboard.
- For lottery permits (Middle Fork, Whitney, etc.), Recreation.gov shows quota for dates that are only available through the lottery. Openings that matter there are cancellations after the lottery, which the app catches as new openings.
- Alerts only. The app never books anything; each alert has a direct booking link.
- Phone numbers are stored for SMS alerts, which will come later through Twilio.

## Command line

```
permit-alerts run                 # dashboard + checker + Discord bot (what the shortcut runs)
permit-alerts find "westwater"    # look up permit/campground IDs
permit-alerts check permit 250014 2027-07-10 2027-07-25          # one-off check
permit-alerts check campground 232445 2027-05-01 2027-05-31 --min-nights 2 --group-size 6
```

## Running on a server

Everything is configured with environment variables (`PERMIT_ALERTS_*`), and
the database can be Postgres instead of the local SQLite file:

```
cp .env.example .env   # fill in DASHBOARD_PASSWORD, Discord, Postgres password
docker compose up -d
```

Set `PERMIT_ALERTS_DASHBOARD_PASSWORD` whenever the dashboard is reachable
from other machines. The browser then asks for it (any username works).

## Development

```
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
python scripts/live_soak.py 5    # live check against Recreation.gov, prints alerts instead of sending
```

Code map: `recgov/client.py` talks to Recreation.gov. `matching.py` applies
the search filters. `checker.py` holds the scheduling, the opening log, and
the alert and reminder rules. `notify/` has the Discord bot and email. `web/`
is the dashboard.
