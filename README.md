# fm26-scout

**A Moneyball analyst for your Football Manager 26 save.**

Point Claude Code at your save and ask it to find the players your scouts are
missing: undervalued signings who outperform your own players on a fraction of
the wage, wonderkids who are already playing senior football, and the true
current and potential ability the game hides from you.

- Reads the `.fm` save directly (read-only). No game mods, nothing injected into the game.
- Python standard library + the `zstd` command-line tool. No `pip install`.
- Sees what the game hides: **true CA/PA**, all 54 attributes including hidden
  ones, and every player's season stats (xG, passes, tackles, headers...).

## Quick start

```bash
git clone https://github.com/ChristianStaubo/fm26-scout.git
cd fm26-scout
claude
```

Then ask in plain English, or use the two built-in commands:

| Command | What it does |
|---|---|
| `/peter-brand` | Your Moneyball analyst: audits your squad, finds its weakest positions, and shortlists undervalued players and wonderkids to fix them |
| `/scout` | Wonderkids, highest CA, best newgens, a club's squad, how a player developed between two saves |

```
/peter-brand "Brighton & Hove Albion", fees up to £25M, wages up to £40K a week
/scout top 20 wonderkids in my latest save
/scout best newgens by current ability
/scout compare Richie Phillips between "online-save-0" and "online-save-2"
```

> **Just the Moneyball analyst, in one line:**
> `git clone https://github.com/ChristianStaubo/fm26-scout.git && cd fm26-scout && claude "/peter-brand"`

Claude finds your newest save, exports it, and answers from the data. Follow-ups
work too: *"who overperformed their xG?"*, *"cheapest left-back with a 7.0+
rating?"*, *"which of these could I actually afford?"*.

## Moneyball

*Named after Peter Brand, the numbers guy from Moneyball.*

`/peter-brand` (or `analyze.py` directly) works like a club analyst:

1. **Squad audit** - each of your regulars is scored against players in the same
   position group (CB, FB, DM, CM, W, AM, ST) in your league: the average
   percentile of that position's key per-90 stats.
2. **Weakest positions** - the groups where even your best player scores lowest.
3. **Moneyball shortlist** - players elsewhere who outscore your best player in
   that position, under your transfer-value and wage ceilings.
4. **Wonderkids** - 21 or under, high PA, already playing senior minutes.

Percentiles are within each player's own league, so an 80 in Ligue 2 is not an
80 in the Premier League - check CA alongside the score.

### Example: Brighton, end of 2025/26

<!-- EXAMPLE: filled in from a real /peter-brand run with screenshots -->

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

# Also export club finances -> exports/<save>_<date>_clubs.csv
python3 fm26_export.py "path/to/save.fm" --clubs

# Squad audit + Moneyball + wonderkid shortlists
python3 analyze.py "exports/<save>_<date>.csv" --club "Brighton & Hove Albion" --max-value 25 --max-wage 40000
```

Saves live in:

- macOS: `~/Library/Application Support/Sports Interactive/Football Manager 26/games/`
- Windows: `Documents\Sports Interactive\Football Manager 26\games\`

If the game is running, it is fine to export - the save is only read - but a
save that is being written at that moment can fail; just run it again.

## What's in the export

One row per player:

- name, date of birth, age, nationality, club, weekly wage, **transfer value**
- **contract**: expiry date, squad status (Important Player, Backup...), date
  joined, agreed renewals, contract offers from other clubs, appearance / goal /
  clean-sheet / assist / unused-sub bonuses
- **loans**: the club that owns the player and, while on loan, the loan club
  and loan end date (`club` is the team he plays for now)
- **CA / PA** (current / potential ability, 1-200)
- natural positions and a 1-20 rating for every position, preferred foot
- all **54 attributes** on the 1-20 scale, including hidden ones (consistency,
  important matches, injury proneness, versatility, dirtiness)
- **season stats** from club competitions: apps, minutes, average rating, goals,
  assists, xG, goals minus xG, shots, passes, crosses, tackles, headers, fouls,
  yellow cards, distance - totals, success % and per 90
- **goalkeeper stats**: clean sheets, goals conceded, saves, shots on target
  faced, save %, and the game's own xSv % and xGP (xG faced minus conceded),
  penalties faced and saved

With `--clubs`, one row per club in a loaded league: latest month-end balance,
transfer budget and amount remaining, wage budget, player wage bill,
sponsorship, and income/expenditure over the last 12 months.

## Vibe coded with Claude Code

This whole project was vibe coded with Claude Code in one session. Instead of
installing an existing save reader, Claude reverse-engineered FM26's save format
from scratch: decompressing the save, diffing two saves taken before and after a
single match, and checking the numbers against screenshots of the in-game screens
until every field matched.

That also means **your own Claude Code can fix it**. If something breaks on
your save, open Claude Code in this folder and describe what you see:

```
fm26_export.py only exported 12 players from my save - find out why and fix it
wages are empty for most players in my save, can you debug it?
```

The format notes and field offsets are documented at the top of
`fm26_export.py`, and the fixes so far all followed the same recipe: find a
player you know, look at his bytes, compare with what the game shows.

## Tested saves (and how they differed)

| Save | Patch | Mode | Age |
|---|---|---|---|
| Portugal | 26.3.1 | single player | ~1 season |
| Brighton | 26.3.2 | single player | 1 season |
| Friends' career | 26.3.2 | online | 8 seasons |

The online career needed four fixes the single-player saves didn't. We can't
tell yet whether that's because it was an online save or because it was 8
seasons old:

- the first-name lookup table was stored in two pieces,
- players without a club use a different marker,
- contracts use a different marker byte,
- the match data kept matches from several seasons.

Another patch or an unusual save may expose new variations - see above for
getting Claude to debug it.

## Known limitations

- Stats only exist for leagues loaded in your save, and cover the most recent
  season. Minutes are derived from substitution times and can be a few % off.
- Leagues other than your own are simulated at lower detail: they record about
  half the events (passes, shots...) and almost no distance. That's why players
  are only ranked against their own league, and why distance isn't used for
  scoring - compare percentiles and CA across leagues, not raw per-90 numbers.
- Some players' club can't be resolved (`club#<id>`, ~15-30% depending on the
  save, mostly smaller clubs and youth sides) and some wages are missing.
- Not exported (yet): personality, injuries, release clauses, trophies. The live
  club balance isn't stored as one number - `--clubs` gives the latest month-end
  balance. Clubs outside loaded leagues have no finances.
- Lower-detail leagues often record no goalkeeper data, so keepers there may
  have partial or no goalkeeper stats. The game's xGP and xSv % are mostly
  noise over a single season: xGP credits the keeper for shots that miss the
  target, and xSv % is inflated for every keeper.
- If your save has women's leagues, women appear in the attribute export;
  Moneyball only uses players with club-competition minutes.

## How it works

- A `.fm` file is a small header followed by zstd frames; the last frame is a
  directory of named internal files.
- `game_db.dat` holds every person. Names are stored in lookup tables; each
  player record holds CA/PA, club, positions and attributes, followed by the
  contract and the name record.
- `match_stats_cfg.dat` holds one compressed blob per match with a stat line per
  player. Season stats are the sum of those lines - which is also how the game
  builds the season stats it shows you.
