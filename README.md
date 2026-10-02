# fm26-scout

Read your **Football Manager 26** save file and find wonderkids, the best players
in your world, the newgens your save has produced - and Moneyball signings,
with an AI analyst to talk you through them.

- Reads the `.fm` save directly (read-only). No game mods, nothing injected into the game.
- Python standard library + the `zstd` command-line tool. No `pip install`.
- Sees what the game hides: **true current/potential ability (CA/PA)**, all 54
  attributes including hidden ones, plus every player's season stats.

## Quick start with Claude Code

```bash
git clone https://github.com/YOUR_USERNAME/fm26-scout.git
cd fm26-scout
claude
```

Then just ask, or use the two built-in commands:

| Command | What it does |
|---|---|
| `/scout` | Wonderkids, highest CA, best newgens, a club's squad, how a player developed between two saves |
| `/peter-brand` | Your Moneyball analyst: audits your squad, finds its weakest positions, and shortlists undervalued players and wonderkids to fix them |

Examples:

```
/scout top 20 wonderkids in my latest save
/scout best newgens by current ability
/scout compare Richie Phillips between "online-save-0" and "online-save-2"
/peter-brand "Brighton & Hove Albion" 40000
```

> **Moneyball only, in one line:**
> `git clone https://github.com/YOUR_USERNAME/fm26-scout.git && cd fm26-scout && claude "/peter-brand"`

Claude finds your newest save, exports it, and answers from the data. Follow-up
questions work too: *"who overperformed their xG?"*, *"cheapest left-back with
a 7.0+ rating?"*, *"which of these could I actually afford?"*.

## Requirements

- Python 3.10+
- `zstd` on your PATH
  - macOS: `brew install zstd`
  - Windows: `winget install zstd` (or Scoop / Chocolatey)
  - Linux: usually preinstalled, otherwise your package manager

## Using the scripts directly

```bash
# Every player in the save -> exports/<save>_<in-game date>.csv
python3 fm26_export.py "path/to/save.fm"

# Wonderkids only
python3 fm26_export.py "path/to/save.fm" --max-age 21 --min-pa 150

# Squad audit + Moneyball + wonderkid shortlists
python3 analyze.py "exports/<save>_<date>.csv" --club "Brighton & Hove Albion" --max-wage 40000
```

Saves live in:

- macOS: `~/Library/Application Support/Sports Interactive/Football Manager 26/games/`
- Windows: `Documents\Sports Interactive\Football Manager 26\games\`

If the game is running, it is fine to export - the save is only read - but a
save that is being written at that moment can fail; just run it again.

## What's in the export

One row per player:

- name, date of birth, age, club, weekly wage
- **CA / PA** (current / potential ability, 1-200)
- natural positions and a 1-20 rating for every position, preferred foot
- all **54 attributes** on the 1-20 scale, including hidden ones (consistency,
  important matches, injury proneness, versatility, dirtiness)
- **season stats** from club competitions: apps, minutes, average rating, goals,
  assists, xG, goals minus xG, shots, passes, crosses, tackles, headers, fouls,
  yellow cards, distance - totals, success % and per 90

## Moneyball

`analyze.py` (and `/peter-brand`) does what Billy Beane's analyst did:

1. **Squad audit** - each of your regulars is scored against players in the same
   position group (CB, FB, DM, CM, W, AM, ST) in your league: the average
   percentile of that position's key per-90 stats.
2. **Weakest positions** - the groups where even your best player scores lowest.
3. **Moneyball shortlist** - players elsewhere who outscore your best player in
   that position, on a wage under your ceiling.
4. **Wonderkids** - 21 or under, high PA, already playing senior minutes.

Percentiles are within each player's own league, so an 80 in Ligue 2 is not an
80 in the Premier League - check CA alongside the score.

## How it works

The save format was reverse-engineered by diffing saves before and after a
match and comparing values with the in-game screens.

- A `.fm` file is a small header followed by zstd frames; the last frame is a
  directory of named internal files.
- `game_db.dat` holds every person. Names are stored in lookup tables; each
  player record holds CA/PA, club, positions and attributes, followed by the
  contract and the name record.
- `match_stats_cfg.dat` holds one compressed blob per match with a stat line per
  player. Season stats are the sum of those lines - which is also how the game
  builds the season stats it shows you.

Field offsets are documented in `fm26_export.py`.

## Known limitations

- Tested on FM26 patches 26.2-26.3.2 with fresh and 8-season-old saves. Another
  patch or an unusual save can expose new variations of the format.
- Stats only exist for leagues loaded in your save, and cover the most recent
  season. Minutes are derived from substitution times and can be a few % off.
- Some players' club can't be resolved (`club#<id>`, ~15-30% depending on the
  save, mostly smaller clubs and youth sides) and some wages are missing.
- Not exported (yet): transfer value, nationality, personality, trophies.
- If your save has women's leagues, women appear in the attribute export;
  Moneyball only uses players with club-competition minutes.

## Something looks wrong on your save?

Open an issue with your FM26 version, roughly how many seasons in you are, and
what looks off (e.g. "exported 12 players", "wages all empty"). Please **don't
attach your save file** - they're large and personal; a description is enough
to start.

## Disclaimer

Unofficial fan project, not affiliated with or endorsed by Sports Interactive
or SEGA. Exported data belongs to them - please don't redistribute it.
