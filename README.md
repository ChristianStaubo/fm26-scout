# fm26-scout

Export player data from a **Football Manager 26** save file to CSV, then find
wonderkids and Moneyball signings.

No third-party Python packages, no game mods, nothing injected into the game:
it reads your `.fm` save file (read-only) with the Python standard library and
the `zstd` command-line tool.

## What you get

`fm26_export.py` writes one row per player:

- name, date of birth, age, club, weekly wage
- **current and potential ability** (CA / PA)
- natural positions plus a 1-20 rating for every position, preferred foot
- all **54 attributes** on the 1-20 scale, including hidden ones
  (consistency, important matches, injury proneness, versatility, dirtiness)
- **season stats** from club competitions: apps, minutes, average rating,
  goals, assists, xG, goals minus xG, shots, passes, crosses, tackles,
  headers, fouls, yellow cards, distance - totals, success % and per-90

`analyze.py` takes that CSV and your club name and prints:

1. a **squad audit** - each regular's position-specific per-90 stats as
   percentiles against his league,
2. **Moneyball** shortlists for your weakest positions - players who outscore
   your best player there on a low wage,
3. **wonderkids** for those positions who already play senior minutes.

## Requirements

- Python 3.10+
- `zstd` on your PATH
  - macOS: `brew install zstd`
  - Windows: `winget install zstd` (or Scoop / Chocolatey)
  - Linux: usually preinstalled, otherwise via your package manager

## Usage

```bash
# Every player in the save
python3 fm26_export.py "path/to/save.fm"

# Wonderkids only
python3 fm26_export.py "path/to/save.fm" --max-age 21 --min-pa 150

# Squad audit + Moneyball + wonderkid shortlists
python3 analyze.py "exports/<save>_<date>.csv" --club "Brighton & Hove Albion" --max-wage 40000
```

CSVs are written to `exports/` and named after the save and in-game date, so
exporting at the end of every season builds up a history.

Saves live in:

- macOS: `~/Library/Application Support/Sports Interactive/Football Manager 26/games/`
- Windows: `Documents\Sports Interactive\Football Manager 26\games\`

If the game is running, copy the save first and export the copy.

## How it works

The format was reverse-engineered by diffing saves and comparing values with
the in-game screens.

- A `.fm` file is a small header followed by zstd frames; the last frame is a
  directory of named internal files.
- `game_db.dat` holds every person. Names are stored in lookup tables; each
  player record holds CA/PA, club, positions and attributes, followed by the
  contract and the name record.
- `match_stats_cfg.dat` holds one compressed blob per match with a stat line
  per player. Season totals are the sum of those lines - the same thing the
  game does when it shows you a player's season stats.

## Limitations

- Tested on FM26 26.3.x saves. A patch that changes the save layout can break it.
- Stats only exist for leagues that were loaded in your save. Minutes are
  derived from substitution times and can be a few percent off.
- Transfer value, nationality and personality are not exported yet.
- About 15% of players at smaller clubs or youth teams show as `club#<id>`.
- If a save includes women's leagues, women appear in the attribute export;
  `analyze.py` only uses players with men's club-competition minutes.

## Disclaimer

Unofficial fan project, not affiliated with or endorsed by Sports Interactive
or SEGA. The exported data belongs to them - please don't redistribute it.
