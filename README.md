# Wipe Review

A small desktop app (dark theme using Discord's colour palette). Paste the URL
of the Warcraft Logs report your raid is live-logging into the **Live log**
field, press **Start**, and a few seconds after each pull ends a result card
appears (a red WIPE or green KILL badge, stat chips, then the review)
explaining why people died:

```
=== WIPE - Ula'tek Heroic pull #1 | 3:50 | boss 42.5% | phase 2 | 27 players ===
  First death 0:57: Vágódiszkó to Caustic Waves. 28 deaths in total, last at 3:49.
  Killed by: Blight Vein x20 (3:15-3:27), Malice x4 (3:48-3:49), Caustic Waves x2 (0:57-0:57)

  Deaths:
  0:57  Laselyb (Arms Warrior) - killed by Caustic Waves, from 100% in 0.3s (one-shot), overkill 84k
        ! had ready (would need pre-using - it was a one-shot): Die by the Sword (not used this pull)
  3:15  Csapdás (Hunter) - killed by Blight Vein, from 61% over 9.4s, overkill 100k
        took: Blight Vein 473k (2x), Necrotic Vapors 287k (10x)
        ! didn't press: Aspect of the Turtle (not used this pull), Exhilaration (not used this pull), Healthstone
```

Addons can't read the combat log in raids any more, so this runs outside the
game and reads Warcraft Logs instead. It uses Python 3.10+ and only the
standard library (Tkinter for the window), so there's nothing to install.

## Running it

```
python app.py
```

Use `pythonw app.py` if you don't want a console window. On first Start it
asks for a Warcraft Logs API client ID and secret. Create a client at
https://www.warcraftlogs.com/api/clients/ (any name, redirect URL
`http://localhost`). The ID and secret are saved to `credentials.local.json`,
which is gitignored.

Someone in the raid needs to log with the Warcraft Logs uploader in **live**
mode, to a **public** or **unlisted** report. Private reports aren't visible
to this kind of API client.

Options are under the gear button next to Start:
- **Include kills** also reviews deaths on kills.
- **Review pulls already in the log** reviews pulls that finished before you
  pressed Start. Without it, only new pulls are reviewed.
- **Keep window on top** keeps it above the game (run WoW in windowed or
  borderless mode).
- **Detailed deaths per pull** sets how many deaths get the full breakdown.
  The rest get one line each.
- **Clear results** empties the results list.

The status bar at the bottom shows what it's doing. The dot is green while it's
watching, yellow while it's reviewing a pull, and red if it stopped on an
error.

A pull is reviewed once the log has run 20s past its end, or its end hasn't
moved for 20s. Every review is also appended to `reviews/<report code>.txt`.

## Command line

```
python -m wipe_review review   <report url> 46          # one or more pulls: 44,46,47
python -m wipe_review replay   <report url> [--kills]   # every pull in a report
python -m wipe_review discover <report url> 44,46,47    # list a boss's ability/debuff/cast IDs
```

## What it checks

- **Killing blow and context.** For each death: what killed them, how fast
  they went from healthy to dead (marked "one-shot" if it took 1.5s or
  less), overkill, damage taken by ability since they were last healthy,
  boss debuffs on them, and defensives or externals active at the killing
  blow.
- **Missed defensives.** These come from `data/defensives.json`, per class
  and spec, matched by spell name. A defensive counts as "ready" if it
  wasn't used this pull or its cooldown had come back. Talented ones only
  count once that player has cast them somewhere in the report. Healthstone
  is included when there's a Warlock in the raid. Cooldowns are
  approximate, so edit them freely.
- **Avoidable damage** and **missed mechanics** come from `data/bosses.json`,
  per encounter ID. It's re-read on every pull, so mid-raid edits apply to
  the next wipe. There are three rule types:
  - `avoidable`: any damage from these ability IDs is a mistake.
  - `debuffs`: when a player gets debuff `id`, they must cast one of `press`
    within `within` seconds, and/or the debuff must be gone within
    `removedWithin` seconds. When 5 or more players fail the same mechanic
    together, it's reported once as a raid-wide failure.
  - `interrupts`: enemy casts that should never complete.

  The boss entries start empty. Run `discover` on a few pulls to find the
  IDs. The `_example` entry shows the format.

## Layout

- `app.py`: the Tkinter window.
- `wipe_review/wcl.py`: Warcraft Logs auth, GraphQL, and batched event fetching.
- `wipe_review/analysis.py`: pull review and discover.
- `wipe_review/watcher.py`: the background thread that polls a live report.
- `data/`: defensives and boss rules.

The API allows 3,600 points per hour. A poll costs very little, and each
review is 2-3 batched queries.
