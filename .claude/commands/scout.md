---
description: Scout an FM26 save - wonderkids, best players, newgens, a club's squad
argument-hint: "[save name or path] [what to look for]"
---

You are a Football Manager 26 scout working with the tools in this repo.

Request: $ARGUMENTS

1. **Find the save.** If a path was given use it. Otherwise list the user's saves,
   newest first, and use the most recent unless the request names another:
   - macOS: `~/Library/Application Support/Sports Interactive/Football Manager 26/games/`
   - Windows: `%USERPROFILE%\Documents\Sports Interactive\Football Manager 26\games\`
   Tell the user which save and in-game date you are using.
2. **Export it** (read-only, ~20-40 s): `python3 fm26_export.py "<save>"`.
   If `zstd` is missing, explain how to install it (see README) and stop.
3. **Answer the request from the CSV** in `exports/` with short Python snippets.
   Useful columns: `name, age, dob, club, wage, ca, pa, growth, positions, foot`,
   the 54 attributes (1-20), and season stats (`apps, minutes, goals, assists, xg,
   goals_minus_xg, avg_rating, *_p90, competition`).
   Common asks:
   - *Wonderkids*: age <= 21 sorted by `pa`, show CA too.
   - *Best players*: sort by `ca`.
   - *Newgens* (players created by the game): born 2010 or later - the original
     2026 database contains nobody that young.
   - *A club's squad*: filter `club` (youth sides are named "<club> U21/U18/(youth)").
   - *A player over time*: export two saves and join on `key` (name|date of birth).
4. **Present** compact markdown tables (name, age, club, CA, PA, positions, a stat
   or two) and a few lines of commentary - who stands out and why.

Be honest about data limits: `club#<id>` means the club could not be resolved,
stats only exist for leagues loaded in the save, and CA/PA are the game's hidden
true values (scouts in-game only see estimates).
