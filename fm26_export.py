#!/usr/bin/env python3
"""Export player data from a Football Manager 26 save (.fm) to CSV.

No third-party Python packages: uses the standard library plus the `zstd`
command-line tool. The save file is opened read-only.

Usage:
    python3 fm26_export.py "path/to/save.fm"                  # all players
    python3 fm26_export.py save.fm --max-age 21 --min-pa 150  # wonderkids

Format notes (reverse-engineered, FM26 v26.x):
  * .fm = 26-byte "fmf." header + zstd frames. The last frame is a directory
    of named entries: (name, ext, offset, compressed size, raw size).
  * game_db.dat holds people. Names live in three lookup tables
    (first names, surnames, common names): [u32 id][u32 len][utf-8].
  * A player's data block comes *before* his name record (see PLAYER_*):
        ... u16 CA, u16 PA ... u16 club index, 6 x 00,
        15 position ratings (1-20), 54 attributes (1-100, display = /5),
        u32 transfer value
    followed (usually ~1-1.4 KB, up to ~12 KB for long careers) by:
        [contract ... u32 weekly wage ...]
        [flag][u32 first][flag][u32 surname][flag][u32 common|-1]
        [flag][u32 len][full name][u16 day-of-year][u16 year of birth]
  * Club records carry their own index near a repeated-u32 marker; clubs
    without the marker are numbered by interpolating between anchors.
  * match_stats_cfg.dat holds one zstd frame per match with a stat record per
    player (see REC_*, STAT_FIELDS, GK_FIELDS). It keeps every match of the
    human manager's club but only ~the last 20 of every other club, so the
    stat columns come from the season-history table where a player can be
    linked to it (full league season, see fm26_history.py) and are the sum of
    the match records otherwise (column stats_source).
  * Contracts, loans, squad status and nationality: see fm26_contracts.py.
    Club finances: see fm26_finance.py.
"""
import argparse
import collections
import csv
import datetime as dt
import os
import re
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fm26_contracts
import fm26_history

HEADER = 26
ZSTD_FRAME = re.compile(re.escape(b'\x28\xb5\x2f\xfd'))
NONE = 0xffffffff

POSITIONS = ['GK', 'SW', 'DL', 'DC', 'DR', 'DM', 'ML', 'MC', 'MR',
             'AML', 'AMC', 'AMR', 'ST', 'WBL', 'WBR']

ATTRIBUTES = [
    'Crossing', 'Dribbling', 'Finishing', 'Heading', 'Long Shots', 'Marking',
    'Off The Ball', 'Passing', 'Penalty Taking', 'Tackling', 'Vision',
    'Handling', 'Aerial Reach', 'Command Of Area', 'Communication', 'Kicking',
    'Throwing', 'Anticipation', 'Decisions', 'One On Ones', 'Positioning',
    'Reflexes', 'First Touch', 'Technique', 'Left Foot', 'Right Foot', 'Flair',
    'Corners', 'Teamwork', 'Work Rate', 'Long Throws', 'Eccentricity',
    'Rushing Out', 'Punching', 'Acceleration', 'Free Kicks', 'Strength',
    'Stamina', 'Pace', 'Jumping Reach', 'Leadership', 'Dirtiness', 'Balance',
    'Bravery', 'Consistency', 'Aggression', 'Agility', 'Important Matches',
    'Injury Proneness', 'Versatility', 'Natural Fitness', 'Determination',
    'Composure', 'Concentration',
]

# Player block, offsets relative to the start of the 6 x 00 (or ff) run.
PLAYER_BLOCK = re.compile(rb'(?:\x00{6}|\xff{6})([\x01-\x14]{15})([\x01-\x64]{54})')  # ff = e.g. no club
PLAYER_INDEX = -37           # u32 player index (match records use index + 1)
PLAYER_CA_PA = -18           # u16 CA, u16 PA
PLAYER_CLUB = -2             # u16 club index
PLAYER_VALUE = 75            # u32 transfer value in £ (the game shows a scouted range around it)
NAME_SEARCH = (80, 12000)    # name record lies this far after the block
CONTRACT_LOOKBACK = 400      # contract lies within this many bytes before the name
# contract: [u16 club][6 x 00][u32 weekly wage][01 xx 00][ff x 4]
CONTRACT = re.compile(rb'(?=(.{2})\x00{6}(.{4})\x01(.)\x00\xff\xff\xff\xff)', re.S)
NAME_CANDIDATE = re.compile(rb'(?=.{16}[\x00-\x78]\x00\x00\x00)', re.S)  # full-name length <= 120

# Club records.
CLUB_RECORD = re.compile(rb'(.{4})(.{4})\2\x00(.{4})\xff\xff\xff\xff\3\3(.{4})(.{4})\x00\x00(.{4})', re.S)
CLUB_MARKER = re.compile(  # repeated u32, 00 00, u32, 5 x 00, flag, [club index][U21 team][U18 team]
    rb'(?=((?!\x00\x00\x00\x00).{4})\1\x00\x00.{4}\x00{5}[\x01-\x0f](.{12}))', re.S)
CLUB_RECORD_SPAN = 8000
MAX_TEAM_INDEX = 100000

# Match stat records, offsets relative to the u32 match id:
#   [02][u32 id][u8 shirt][u8 side 0=home/1=away][02][u32][02][u32][u8][u8 fit start][u8 fit end]
MATCH_RECORD = re.compile(rb'(?=\x02(.{4})[\x00-\x63][\x00\x01]\x02.{4}\x02.{4}.[\x00-\x64][\x00-\x64])', re.S)
MATCH_TEAMS = re.compile(rb'\x03\x02(.{4})\x02', re.S)
MATCH_SEASON = 19           # u16 season start year in the match header
# Competition: [u32 len][full name][u32 len][short name], e.g. "Italian Serie A" / "Serie A". The full
# name is used (short names collide: English and Portuguese "Premier League").
MATCH_COMP = re.compile(rb'(?=[\x03-\x40]\x00\x00\x00[A-Z])')
NAME_BYTES = re.compile(rb'[^\x00-\x1f]+')
REC_LEN = 230
REC_SIDE = 5
REC_SUB_MARK = 16            # 255 for a regular record
REC_BENCH = 69               # 100 + no distance + a 6.40 rating = unused substitute (lower-detail matches)
REC_UNUSED = 98              # 100 = unused substitute
REC_SUB_TIMES = 100          # u32 on, u32 off, in quarter-seconds (NONE = not subbed)
REC_RATING = 135             # u16 rating x 100
REC_CHECK = 138
STAT_FIELDS = {  # name: (offset, struct format) - verified against in-game squad totals
    'goals': (19, 'B'), 'xg': (24, '<f'), 'shots': (40, 'B'), 'shots_on_target': (41, 'B'),
    'assists': (55, 'B'), 'dribbles': (57, 'B'), 'fouls': (59, 'B'), 'yellow_cards': (62, 'B'),
    'progressive_passes': (177, 'B'), 'xa': (182, '<f'),  # xA: 186 holds a variant without set pieces (unverified)
    'interceptions': (97, 'B'), 'clearances': (152, 'B'), 'shots_blocked': (54, 'B'), 'key_tackles': (93, 'B'),
    'poss_won': (150, 'B'), 'poss_lost': (151, 'B'), 'pressures_att': (159, 'B'), 'pressures_cmp': (160, 'B'),
    'mistakes_to_goal': (143, 'B'),
    'passes_att': (85, 'B'), 'passes_cmp': (86, 'B'), 'crosses_att': (88, 'B'), 'crosses_cmp': (89, 'B'),
    'tackles_att': (91, 'B'), 'tackles_won': (92, 'B'), 'headers_att': (94, 'B'), 'headers_won': (95, 'B'),
    'distance_m': (139, '<f'),
}
GK_FIELDS = {  # recorded while the player is in goal - verified against the in-game goalkeeper stats
    'conceded': (20, 'B'), 'xg_faced': (28, '<f'), 'xgot_faced': (32, '<f'),
    'saves_held': (49, 'B'), 'saves_parried': (50, 'B'), 'saves_tipped': (51, 'B'),
    'pens_faced': (52, 'B'), 'pens_saved': (53, 'B'), 'sot_faced': (156, 'B'),
}
CLEAN_SHEET_MINUTES = 60


def u32(buf, off=0):
    return struct.unpack_from('<I', buf, off)[0]


def fm_date(year, day_of_year):
    return dt.date(year, 1, 1) + dt.timedelta(day_of_year - 1)


def frame_starts(data):
    return [m.start() for m in ZSTD_FRAME.finditer(data)]


def zstd_decompress(data):
    return subprocess.run(['zstd', '-d', '-c', '-q'], input=data, capture_output=True).stdout


# --- Save container ------------------------------------------------------------

def _directory_entry(t, i):
    """Parse [u32 len][name][u32 len][ext][u64 offset][u64 csize][u64 size] at i, or None."""
    try:
        n = u32(t, i)
        if not 0 < n <= 200:
            return None
        name = t[i + 4:i + 4 + n].decode('ascii')
        j = i + 4 + n
        m = u32(t, j)
        ext = t[j + 4:j + 4 + m].decode('ascii')
        j += 4 + m
        if not re.fullmatch(r'[\w\-]+', name) or not re.fullmatch(r'\.\w+', ext):
            return None
        return name + ext, struct.unpack_from('<QQQ', t, j), j + 24
    except (UnicodeDecodeError, struct.error):
        return None


def read_directory(raw):
    for s in reversed(frame_starts(raw)):
        t = zstd_decompress(raw[s:])
        if b'game_info' in t[:300]:
            break
    else:
        sys.exit('Could not find the save directory - is this an FM26 save?')
    entries, i = {}, 4 + u32(t)  # skip the save title
    while i < len(t) - 8:
        entry = _directory_entry(t, i)
        if entry:
            name, location, i = entry
            entries[name] = location
        else:
            i += 1
    return entries


def read_entry(raw, entries, name):
    off, csize, _ = entries[name]
    return zstd_decompress(raw[HEADER + off:HEADER + off + csize])


def game_date(summary):
    """The summary holds one [u16 day-of-year | time << 9][u16 year] - the current date.
    Other bytes can look like a date too (a save dated 2031-01-08 had a false 2048-01-02 three bytes
    earlier, which made every age 17 years too old): take the year most candidates agree on, then
    its first candidate."""
    found = []
    for i in range(len(summary) - 3):
        v, year = struct.unpack_from('<HH', summary, i)
        if 2025 <= year <= 2100 and 1 <= v & 0x1ff <= 366:
            found.append((year, v & 0x1ff))
    if not found:
        return None
    years = [y for y, _ in found]
    best = max(years, key=lambda y: (years.count(y), -years.index(y)))
    return fm_date(*next(c for c in found if c[0] == best))


# --- People and clubs ----------------------------------------------------------

def name_tables(db):
    """Find the three big [id][len][str] tables: first names, surnames, common names."""
    tables, seen = [], 0
    for m in re.finditer(rb'[\x01-\x40]\x00\x00\x00[A-Z\xc3\xc4\xc5]', db):
        s = m.start() - 4
        if s < seen:
            continue
        j, prev, ents = s, None, {}
        while j + 8 <= len(db):
            idv, ln = struct.unpack_from('<II', db, j)
            if ln == 0 or ln > 80 or (prev is not None and idv != prev + 1):
                break
            try:
                ents[idv] = db[j + 8:j + 8 + ln].decode('utf-8')
            except UnicodeDecodeError:
                break
            prev, j = idv, j + 8 + ln
        if len(ents) >= 200:
            seen = j
            if 0 not in ents and tables:  # tables can be split in pieces; each table starts at id 0
                tables[-1].update(ents)
                continue
            if len(tables) == 3:
                return tables
            tables.append(ents)
    if len(tables) == 3:
        return tables
    sys.exit('Could not locate name tables.')


def parse_name(db, p, first_names, surnames, common_names):
    """Return (first, last, common, full, dob) if a name record starts at p."""
    f, s, c, ln = u32(db, p + 1), u32(db, p + 6), u32(db, p + 11), u32(db, p + 16)
    if f == 0 or f not in first_names or s == 0 or s not in surnames:  # id 0 would match zero runs
        return None
    if not (c == NONE or (c != 0 and c in common_names)) or ln > 120:
        return None
    try:
        full = db[p + 20:p + 20 + ln].decode('utf-8')
    except UnicodeDecodeError:
        return None
    if not full.isprintable():
        return None
    doy, year = struct.unpack_from('<HH', db, p + 20 + ln)
    if not (1900 < year < 2100 and 0 < doy <= 366):
        return None
    return first_names[f], surnames[s], common_names.get(c), full, fm_date(year, doy)


def club_index(db):
    """Map club/team index -> club name (U21/U18 squads get a suffix)."""
    recs = []
    for m in CLUB_RECORD.finditer(db):
        ln = u32(m.group(6))
        if not 0 < ln < 100:
            continue
        try:
            name = db[m.end():m.end() + ln].decode()
        except UnicodeDecodeError:
            continue
        if name[:1].isalnum():
            recs.append((m.start(), u32(m.group(1)), name))

    idx, anchors = {}, []
    for i, (start, _, name) in enumerate(recs):
        end = recs[i + 1][0] if i + 1 < len(recs) else start + CLUB_RECORD_SPAN
        found = {struct.unpack('<III', m.group(2)) for m in CLUB_MARKER.finditer(db, start, end)}
        found = {f for f in found if f[0] < MAX_TEAM_INDEX}
        if len(found) != 1:
            continue
        (club, u21, u18), = found
        idx.setdefault(club, name)
        anchors.append((i, club))
        if 0 < u21 < MAX_TEAM_INDEX:
            if u18 == u21 + 1:
                idx.setdefault(u21, f'{name} U21')
                idx.setdefault(u18, f'{name} U18')
            else:
                idx.setdefault(u21, f'{name} (youth)')

    # Clubs without a marker: number them from the record sequence between two anchors.
    for (i, a), (j, b) in zip(anchors, anchors[1:]):
        xa, xb = recs[i][1], recs[j][1]
        if b - a == xb - xa:
            for k in range(i + 1, j):
                idx.setdefault(a + recs[k][1] - xa, recs[k][2])
    return idx


# --- Season match stats --------------------------------------------------------

def match_frames(raw, entries):
    """Decompress every per-match frame of match_stats_cfg.dat (in order)."""
    if 'match_stats_cfg.dat' not in entries:
        return []
    data = read_entry(raw, entries, 'match_stats_cfg.dat')
    starts = frame_starts(data) + [len(data)]
    with ThreadPoolExecutor(os.cpu_count()) as pool:
        return list(pool.map(zstd_decompress, (data[a:b] for a, b in zip(starts, starts[1:]))))


def minutes_played(rec):
    on, off = struct.unpack_from('<II', rec, REC_SUB_TIMES)
    if rec[REC_UNUSED] == 100 and on == NONE:
        return 0  # unused substitute
    if (rec[REC_BENCH] == 100 and struct.unpack_from('<H', rec, REC_RATING)[0] == 640
            and struct.unpack_from('<f', rec, STAT_FIELDS['distance_m'][0])[0] == 0):
        return 0  # unused substitute in a lower-detail match
    start = 0 if on == NONE else on / 240
    end = 90 if off == NONE else min(off / 240, 90)
    return max(1, end - start)


def match_records(x):
    """Best stat record per match id in one match frame -> {match_id: (rating, record)}."""
    best = {}
    for m in MATCH_RECORD.finditer(x):
        o = m.start() + 1
        if o + REC_LEN > len(x):
            continue
        rating = struct.unpack_from('<H', x, o + REC_RATING)[0]
        if not (rating == 0 or 300 <= rating <= 1000) or x[o + REC_CHECK] > 9:
            continue
        mid = u32(m.group(1))
        score = (x[o + REC_SUB_MARK] == 255, rating > 0)
        if mid not in best or score > best[mid][0]:
            best[mid] = (score, rating, x[o:o + REC_LEN])
    return {mid: (rating, rec) for mid, (_, rating, rec) in best.items()}


def match_header(x):
    """(home/away team indices, competition name or None, season year) of one match frame."""
    found = list(MATCH_TEAMS.finditer(x[:600]))[:2]
    teams = [u32(m.group(1)) for m in found]
    comp = competition_name(x[:found[0].start() if found else 600])  # before the teams (the referee comes later)
    return teams, comp, struct.unpack_from('<H', x, MATCH_SEASON)[0]


def competition_name(header):
    """Full competition name: the first [u32 len][name][u32 len][short name] pair in a match header."""
    for m in MATCH_COMP.finditer(header):
        i = m.start()
        full, j = header[i + 4:i + 4 + header[i]], i + 4 + header[i]
        if j + 4 > len(header) or not 3 <= header[j] <= 64 or header[j + 1:j + 4] != b'\x00\x00\x00':
            continue
        short = header[j + 4:j + 4 + header[j]]
        if len(short) == header[j] and NAME_BYTES.fullmatch(full) and NAME_BYTES.fullmatch(short):
            return full.decode('utf-8', 'replace')
    return None


def club_matches(frames):
    """(frame, team indices, competition, season) of every non-friendly match."""
    matches = [(x, *match_header(x)) for x in frames if len(x) > MATCH_SEASON + 2]
    return [m for m in matches if len(m[1]) == 2 and m[2] != 'Friendly']


def main_seasons(matches):
    """Competition -> the season (start year) with the most matches in the save."""
    main_season = {}
    for (comp, season), _ in collections.Counter((comp, season) for _, _, comp, season in matches).most_common():
        main_season.setdefault(comp, season)
    return main_season


def season_stats(frames, clubs, matches=None):
    """Sum club-competition match records per player index (friendlies/internationals excluded).

    Saves keep some matches from neighbouring seasons, so per competition only the
    season with the most matches is counted. For clubs other than the human
    manager's, the save only keeps about the last 20 matches (see fm26_history).
    """
    if matches is None:
        matches = club_matches(frames)
    main_season = main_seasons(matches)
    totals = {}
    for x, teams, comp, season in matches:
        if season != main_season[comp]:
            continue
        for mid, (rating, rec) in match_records(x).items():
            if teams[rec[REC_SIDE]] not in clubs:  # player's side is a national team
                continue
            mins = minutes_played(rec)
            if mins == 0:
                continue
            t = totals.setdefault(mid - 1, {**dict.fromkeys(['apps', 'minutes', 'rating_sum', *STAT_FIELDS,
                                                             'gk_apps', 'gk_minutes', 'clean_sheets', *GK_FIELDS], 0),
                                            'comps': {}})
            if comp:
                t['comps'][comp] = t['comps'].get(comp, 0) + mins
            t['apps'] += 1
            t['minutes'] += mins
            t['rating_sum'] += rating / 100
            for k, (off, fmt) in STAT_FIELDS.items():
                t[k] += struct.unpack_from(fmt, rec, off)[0]
            gk = {k: struct.unpack_from(fmt, rec, off)[0] for k, (off, fmt) in GK_FIELDS.items()}
            if not 0 <= gk['xgot_faced'] <= 10:
                gk['xgot_faced'] = 0.0  # a few records hold garbage (~1e27) here
            if gk['sot_faced'] or gk['conceded'] or gk['xg_faced'] > 0:  # in goal (lower-detail matches lack GK data)
                t['gk_apps'] += 1
                t['gk_minutes'] += mins
                t['clean_sheets'] += gk['conceded'] == 0 and mins >= CLEAN_SHEET_MINUTES
                for k, v in gk.items():
                    t[k] += v
    return totals


def gk_columns(t):
    if not t['gk_minutes']:
        return {}
    n90 = t['gk_minutes'] / 90
    sot = t['sot_faced']
    saves = t['saves_held'] + t['saves_parried'] + t['saves_tipped']
    # The game's xGP: xG of every shot faced (on target or not) minus goals conceded - matches the in-game
    # value. Mostly noise over one season (it credits the keeper for shots that miss the target).
    xgp = t['xg_faced'] - t['conceded']
    return {
        'gk_apps': t['gk_apps'], 'clean_sheets': t['clean_sheets'], 'conceded': t['conceded'],
        'saves': saves, 'sot_faced': sot,
        'save_pct': round(100 * saves / sot, 1) if sot else None,
        'xsave_pct': round(100 * (1 - t['xgot_faced'] / sot), 1) if sot else None,  # the game's (inflated) xSv %
        'xgp': round(xgp, 2), 'xg_faced': round(t['xg_faced'], 2),
        'pens_faced': t['pens_faced'], 'pens_saved': t['pens_saved'],
        'conceded_p90': round(t['conceded'] / n90, 2), 'saves_p90': round(saves / n90, 2),
        'xgp_p90': round(xgp / n90, 2),
    }


def stat_columns(t):
    if not t:
        return {'competition': '', 'apps': 0, 'minutes': 0}
    n90 = t['minutes'] / 90
    km = t['distance_m'] / 1000

    def per90(v):
        return round(v / n90, 2)

    def pct(made, attempted):
        return round(100 * made / attempted) if attempted else None

    return {
        'competition': max(t['comps'], key=t['comps'].get) if t['comps'] else '',
        'apps': t['apps'], 'minutes': round(t['minutes']),
        'avg_rating': round(t['rating_sum'] / t['apps'], 2),
        'goals': t['goals'], 'assists': t['assists'], 'xg': round(t['xg'], 2), 'xa': round(t['xa'], 2),
        'goals_minus_xg': round(t['goals'] - t['xg'], 2),
        'shots': t['shots'], 'shots_on_target': t['shots_on_target'],
        'passes_att': t['passes_att'], 'pass_pct': pct(t['passes_cmp'], t['passes_att']),
        'crosses_att': t['crosses_att'], 'cross_pct': pct(t['crosses_cmp'], t['crosses_att']),
        'tackles_att': t['tackles_att'], 'tackle_pct': pct(t['tackles_won'], t['tackles_att']),
        'headers_att': t['headers_att'], 'header_pct': pct(t['headers_won'], t['headers_att']),
        'fouls': t['fouls'], 'yellow_cards': t['yellow_cards'],
        'distance_km': round(km, 1),
        'goals_p90': per90(t['goals']), 'assists_p90': per90(t['assists']), 'xg_p90': per90(t['xg']),
        'shots_p90': per90(t['shots']), 'passes_cmp_p90': per90(t['passes_cmp']),
        'xa_p90': per90(t['xa']), 'progressive_passes_p90': per90(t['progressive_passes']),
        'dribbles_p90': per90(t['dribbles']),
        'interceptions': t['interceptions'], 'clearances': t['clearances'], 'shots_blocked': t['shots_blocked'],
        'key_tackles': t['key_tackles'], 'mistakes_to_goal': t['mistakes_to_goal'],
        'pressures_att': t['pressures_att'], 'pressure_pct': pct(t['pressures_cmp'], t['pressures_att']),
        'interceptions_p90': per90(t['interceptions']), 'clearances_p90': per90(t['clearances']),
        'shots_blocked_p90': per90(t['shots_blocked']), 'poss_won_p90': per90(t['poss_won']),
        'poss_lost_p90': per90(t['poss_lost']), 'pressures_att_p90': per90(t['pressures_att']),
        'headers_att_p90': per90(t['headers_att']),
        'tackles_won_p90': per90(t['tackles_won']), 'headers_won_p90': per90(t['headers_won']),
        'crosses_cmp_p90': per90(t['crosses_cmp']), 'distance_p90': per90(km),
        **gk_columns(t),
    }


# --- Full-season league stats (season history) ------------------------------------
#
# match_stats_cfg.dat keeps every match of the human manager's club, but only about the last 20
# matches (roughly February onwards) of every other club, so the sums above are partial for most
# players. Where a player can be linked to his full league season in the history table
# (fm26_history), the stat columns hold that season instead: league only, every count the record
# holds. Distance is not in the record: distance_km / distance_p90 stay on the per-match lines
# (per 90 over window_minutes). stats_source says which source a row uses.

HISTORY_WINDOW_FIELDS = {'goals': 19, 'assists': 55, 'shots': 40, 'passes_att': 85, 'tackles_att': 91,
                         'interceptions': 97}  # u8 offsets in a match record (see STAT_FIELDS)


def season_history(raw, entries, matches, clubs):
    """fm26_history.SeasonHistory for this save, or None if the save lacks the tables.

    Only leagues whose matches are in the save (named by them) and only the season the per-match
    data covers (main_seasons) are linked, so a row never mixes seasons or holds a stale one."""
    if fm26_history.HIST_DT not in entries or fm26_history.CURRENT not in entries:
        return None
    hist = fm26_history.SeasonHistory(read_entry(raw, entries, fm26_history.HIST_DT),
                                      read_entry(raw, entries, fm26_history.CURRENT))
    hist.league_names = fm26_history.league_names(hist.leagues, [m[1:] for m in matches])
    seasons = main_seasons(matches)
    # The season to link for a club: the one its league's per-match data covers, once it is closed.
    hist.season_of = lambda club: next((y for y in hist.years if hist.league_name(club, y)
                                        and seasons.get(hist.league_name(club, y)) == y and hist.complete(club, y)), None)
    # Partial (per-match) league totals: a matched record must contain them (see SeasonHistory.candidates).
    window = collections.defaultdict(collections.Counter)
    for x, teams, comp, season in matches:
        for mid, (_, rec) in match_records(x).items():
            team = teams[rec[REC_SIDE]]
            if team not in clubs or comp != hist.league_name(team, season) or minutes_played(rec) == 0:
                continue
            w = window[(mid - 1, team, season)]
            w['minutes'] += minutes_played(rec)
            for k, off in HISTORY_WINDOW_FIELDS.items():
                w[k] += rec[off]
    hist.window = window
    hist.teams = collections.defaultdict(list)  # player index -> clubs he played league games for, most minutes first
    for (idx, team, _), w in sorted(window.items(), key=lambda kv: -kv[1]['minutes']):
        hist.teams[idx].append(team)
    return hist


def history_totals(hist, idx, keeper=False):
    """(totals, club, season) of the player's full league season from the history table, or None."""
    found = hist.find(idx) if hist else None
    if not found:
        return None
    team, year, rec = found
    t = fm26_history.record_totals(rec, keeper)
    if not t['minutes']:
        return None
    t['comps'] = {hist.league_name(team, year): t['minutes']}
    return t, team, year


EXTRA_STAT_COLUMNS = ('stats_source', 'window_apps', 'window_minutes', 'starts', 'season_year', 'stats_club')


def player_stat_columns(window, hist, idx, clubs, keeper=False):
    """Stat columns of one player: his full league season if the history links him, else his
    per-match (window) totals. Extra columns: stats_source (history | window | empty), window_apps
    and window_minutes (what the per-match lines hold, all club competitions), and for history rows
    starts, season_year and stats_club (the club of that league season)."""
    found = history_totals(hist, idx, keeper)
    w_apps = window['apps'] if window else 0
    w_min = round(window['minutes']) if window else 0
    if not found:
        return {**stat_columns(window), 'stats_source': 'window' if window else '',
                'window_apps': w_apps, 'window_minutes': w_min}
    t, team, year = found
    cols = stat_columns({k: 0 if v is None else v for k, v in t.items()})
    if keeper:  # these record slots hold saves and penalties
        cols.update(dict.fromkeys(('headers_att', 'header_pct', 'headers_att_p90', 'headers_won_p90',
                                   'tackles_att', 'tackle_pct', 'tackles_won_p90')))
    km = window['distance_m'] / 1000 if window and window['minutes'] else None
    cols.update(distance_km=round(km, 1) if km is not None else None,
                distance_p90=round(km / (window['minutes'] / 90), 2) if km is not None else None)
    return {**cols, 'stats_source': 'history', 'window_apps': w_apps, 'window_minutes': w_min,
            'starts': t['starts'], 'season_year': year, 'stats_club': clubs.get(team, f'club#{team}')}


# --- Players -------------------------------------------------------------------

def find_name(db, start, end, names):
    """First name record in [start, end), as (offset, parsed name), or None."""
    for m in NAME_CANDIDATE.finditer(db, start, end + 20):
        q = m.start()
        if q >= end:
            return None
        person = parse_name(db, q, *names)
        if person:
            return q, person
    return None


def find_wage(db, start, end, club, clubs):
    """Weekly wage from the player's contract.

    The marker byte is usually 0x0b; other values only count when the contract's
    club is the player's club or its parent (youth players' contracts name the
    parent club).
    """
    contracts = [(struct.unpack('<H', m.group(1))[0], u32(m.group(2)), m.group(3)) for m in CONTRACT.finditer(db, start, end)]
    usual = [wage for _, wage, mark in contracts if mark == b'\x0b']
    if usual:
        return usual[-1]
    team = clubs.get(club, '')
    matching = [wage for c, wage, _ in contracts if c == club or (c in clubs and team.startswith(clubs[c] + ' '))]
    return matching[-1] if matching else None


def parse_player(db, block, end, names, clubs, stats, today, history=None):
    p = block.start()
    found = find_name(db, p + NAME_SEARCH[0], end, names)
    if not found:
        return None
    q, (first, last, common, full, dob) = found
    club = struct.unpack_from('<H', db, p + PLAYER_CLUB)[0]
    contract = fm26_contracts.contract_status(db, p, q, club, clubs)
    wage = contract.pop('wage', None)
    if wage is None:
        wage = find_wage(db, max(p, q - CONTRACT_LOOKBACK), q, club, clubs)
    _, nation = fm26_contracts.nationality(db, q)
    ca, pa = struct.unpack_from('<HH', db, p + PLAYER_CA_PA)
    pos = dict(zip(POSITIONS, block.group(1)))
    attrs = dict(zip(ATTRIBUTES, block.group(2)))
    left, right = attrs['Left Foot'], attrs['Right Foot']
    full_name = full or f'{first} {last}'
    born = dob.isoformat()
    age = None
    if today:
        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    row = {
        'key': f'{full_name}|{born}',
        'name': common or f'{first} {last}',
        'full_name': full_name,
        'dob': born, 'age': age,
        'club': 'Free agent' if club == 0xffff else clubs.get(club, f'club#{club}'),  # loan club while on loan
        'nation': nation,
        'wage': wage,
        'contract_expires': contract.get('contract_expires'),
        'squad_status': contract.get('squad_status'),
        'contract_club': contract.get('contract_club'),
        'on_loan_at': contract.get('on_loan_at'), 'loan_ends': contract.get('loan_ends'),
        'value': u32(db, p + PLAYER_VALUE),
        'ca': ca, 'pa': pa, 'growth': pa - ca,
        'positions': '/'.join(n for n, r in pos.items() if r == 20),
        'foot': 'Either' if min(left, right) >= 75 else ('Left' if left > right else 'Right'),
    }
    row.update({k: contract.get(k) for k in ('joined_club', 'renewal_expires', 'contract_offers',
                                              *fm26_contracts.BONUS_TYPES.values())})
    idx = u32(db, p + PLAYER_INDEX)
    stat_cols = player_stat_columns(stats.get(idx), history, idx, clubs, pos['GK'] == 20)
    extra = {k: stat_cols.pop(k) for k in EXTRA_STAT_COLUMNS if k in stat_cols}  # new columns go last
    row.update(stat_cols)
    row.update({f'pos_{n}': r for n, r in pos.items() if n != 'SW'})
    row.update({a: round(v / 5) for a, v in attrs.items()})
    row.update(extra)
    return row


def extract(path, finances=False):
    raw = Path(path).read_bytes()
    entries = read_directory(raw)
    today = game_date(read_entry(raw, entries, 'save_game_summary.dat'))
    print(f'  in-game date: {today}', file=sys.stderr)
    frames = match_frames(raw, entries)  # before the big db: subprocesses spawn faster from a small process
    db = read_entry(raw, entries, 'game_db.dat')
    print(f'  database: {len(db) / 1e6:.0f} MB decompressed', file=sys.stderr)
    names = name_tables(db)
    clubs = club_index(db)
    matches = club_matches(frames)
    stats = season_stats(frames, clubs, matches)
    print(f'  season stats: {len(stats)} players with club appearances', file=sys.stderr)
    history = season_history(raw, entries, matches, clubs)

    blocks = list(PLAYER_BLOCK.finditer(db))
    if history:
        player_clubs = {}
        for block in blocks:
            idx, club = u32(db, block.start() + PLAYER_INDEX), struct.unpack_from('<H', db, block.start() + PLAYER_CLUB)[0]
            player_clubs[idx] = [club, *history.teams.get(idx, ())]
        history.link(player_clubs)
    players = []
    for k, block in enumerate(blocks):
        end = block.start() + NAME_SEARCH[1]
        if k + 1 < len(blocks):
            end = min(end, blocks[k + 1].start())
        row = parse_player(db, block, end, names, clubs, stats, today, history)
        if row:
            players.append(row)
    if history:
        src = collections.Counter(p['stats_source'] for p in players)
        print(f"  full league seasons: {src['history']} players from the season history, "
              f"{src['window']} on per-match data only", file=sys.stderr)
    if finances:
        import fm26_finance
        return players, today, fm26_finance.all_finances(db, today)
    return players, today


def club_rows(players, finances):
    """One row per club with finances, plus its player wage bill (loaned-out players excluded)."""
    squads = collections.defaultdict(list)
    for p in players:
        if p['contract_club'] and not p['on_loan_at']:
            squads[p['contract_club']].append(p)
    return [{'club': name, **f,
             'player_wages_pw': sum(p['wage'] or 0 for p in squads[name]),
             'players': len(squads[name]),
             'loaned_out': sum(1 for p in players if p['contract_club'] == name and p['on_loan_at'])}
            for name, f in finances.items()]


def write_csv(path, rows):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        # Rows can have different columns (e.g. players without stats), so take the union in first-seen order.
        w = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for r in rows for k in r)))
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('save', help='path to the .fm save file')
    ap.add_argument('-o', '--out', help='output CSV (default: exports/<save>_<date>.csv)')
    ap.add_argument('--max-age', type=int, help='only players this age or younger')
    ap.add_argument('--min-pa', type=int, help='only players with at least this potential ability')
    ap.add_argument('--clubs', action='store_true', help='also write club finances to <output>_clubs.csv')
    a = ap.parse_args()

    print(f'Reading {a.save}', file=sys.stderr)
    players, today, *finances = extract(a.save, a.clubs)
    if a.clubs:
        clubs_rows = club_rows(players, finances[0])
    if a.max_age is not None:
        players = [p for p in players if p['age'] is not None and p['age'] <= a.max_age]
    if a.min_pa is not None:
        players = [p for p in players if p['pa'] >= a.min_pa]
    players.sort(key=lambda p: (-p['pa'], -p['ca']))

    out = a.out
    if not out:
        tag = '_wonderkids' if a.max_age is not None or a.min_pa is not None else ''
        out = Path(__file__).resolve().parent / 'exports' / f'{Path(a.save).stem}_{today}{tag}.csv'
    Path(out).resolve().parent.mkdir(parents=True, exist_ok=True)
    write_csv(out, players)
    print(f'Wrote {len(players)} players -> {out}', file=sys.stderr)
    if a.clubs:
        clubs_out = Path(out).with_name(Path(out).stem + '_clubs.csv')
        write_csv(clubs_out, clubs_rows)
        print(f'Wrote {len(clubs_rows)} clubs -> {clubs_out}', file=sys.stderr)


if __name__ == '__main__':
    main()
