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
        15 position ratings (1-20), 54 attributes (1-100, display = /5)
    followed (usually ~1-1.4 KB, up to ~12 KB for long careers) by:
        [contract ... u32 weekly wage ...]
        [flag][u32 first][flag][u32 surname][flag][u32 common|-1]
        [flag][u32 len][full name][u16 day-of-year][u16 year of birth]
  * Club records carry their own index near a repeated-u32 marker; clubs
    without the marker are numbered by interpolating between anchors.
  * match_stats_cfg.dat holds one zstd frame per match with a stat record per
    player (see REC_*). Season stats are the sum of those records.
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
NAME_SEARCH = (80, 12000)    # name record lies this far after the block
CONTRACT_LOOKBACK = 400      # contract lies within this many bytes before the name
CONTRACT = re.compile(rb'.{4}\x00{6}(.{4})\x01\x0b\x00\xff\xff\xff\xff', re.S)  # group 1 = wage
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
MATCH_COMP = re.compile(rb'[\x03-\x40]\x00\x00\x00([A-Z][^\x00-\x1f]{2,60}?)[\x03-\x40]\x00\x00\x00\1', re.S)
REC_LEN = 230
REC_SIDE = 5
REC_SUB_MARK = 16            # 255 for a regular record
REC_UNUSED = 98              # 100 = unused substitute
REC_SUB_TIMES = 100          # u32 on, u32 off, in quarter-seconds (NONE = not subbed)
REC_RATING = 135             # u16 rating x 100
REC_CHECK = 138
STAT_FIELDS = {  # name: (offset, struct format) - verified against in-game squad totals
    'goals': (19, 'B'), 'xg': (24, '<f'), 'shots': (40, 'B'), 'shots_on_target': (41, 'B'),
    'assists': (55, 'B'), 'fouls': (59, 'B'), 'yellow_cards': (62, 'B'),
    'passes_att': (85, 'B'), 'passes_cmp': (86, 'B'), 'crosses_att': (88, 'B'), 'crosses_cmp': (89, 'B'),
    'tackles_att': (91, 'B'), 'tackles_won': (92, 'B'), 'headers_att': (94, 'B'), 'headers_won': (95, 'B'),
    'distance_m': (139, '<f'),
}


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
    """The summary holds one [u16 day-of-year | time << 9][u16 year] - the current date."""
    for i in range(len(summary) - 3):
        v, year = struct.unpack_from('<HH', summary, i)
        if 2025 <= year <= 2100 and 1 <= v & 0x1ff <= 366:
            return fm_date(year, v & 0x1ff)
    return None


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
    teams = [u32(m.group(1)) for m in MATCH_TEAMS.finditer(x[:600])][:2]
    comp = MATCH_COMP.search(x[:600])
    comp = comp.group(1).decode('utf-8', 'replace') if comp else None
    return teams, comp, struct.unpack_from('<H', x, MATCH_SEASON)[0]


def season_stats(frames, clubs):
    """Sum club-competition match records per player index (friendlies/internationals excluded).

    Saves keep some matches from neighbouring seasons, so per competition only the
    season with the most matches is counted.
    """
    matches = [(x, *match_header(x)) for x in frames if len(x) > MATCH_SEASON + 2]
    matches = [m for m in matches if len(m[1]) == 2 and m[2] != 'Friendly']
    per_season = collections.Counter((comp, season) for _, _, comp, season in matches)
    main_season = {}
    for (comp, season), n in per_season.most_common():
        main_season.setdefault(comp, season)
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
            t = totals.setdefault(mid - 1, {**dict.fromkeys(['apps', 'minutes', 'rating_sum', *STAT_FIELDS], 0),
                                            'comps': {}})
            if comp:
                t['comps'][comp] = t['comps'].get(comp, 0) + mins
            t['apps'] += 1
            t['minutes'] += mins
            t['rating_sum'] += rating / 100
            for k, (off, fmt) in STAT_FIELDS.items():
                t[k] += struct.unpack_from(fmt, rec, off)[0]
    return totals


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
        'goals': t['goals'], 'assists': t['assists'], 'xg': round(t['xg'], 2),
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
        'tackles_won_p90': per90(t['tackles_won']), 'headers_won_p90': per90(t['headers_won']),
        'crosses_cmp_p90': per90(t['crosses_cmp']), 'distance_p90': per90(km),
    }


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


def parse_player(db, block, end, names, clubs, stats, today):
    p = block.start()
    found = find_name(db, p + NAME_SEARCH[0], end, names)
    if not found:
        return None
    q, (first, last, common, full, dob) = found
    contracts = list(CONTRACT.finditer(db, max(p, q - CONTRACT_LOOKBACK), q))
    ca, pa = struct.unpack_from('<HH', db, p + PLAYER_CA_PA)
    club = struct.unpack_from('<H', db, p + PLAYER_CLUB)[0]
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
        'club': 'Free agent' if club == 0xffff else clubs.get(club, f'club#{club}'),
        'wage': u32(contracts[-1].group(1)) if contracts else None,
        'ca': ca, 'pa': pa, 'growth': pa - ca,
        'positions': '/'.join(n for n, r in pos.items() if r == 20),
        'foot': 'Either' if min(left, right) >= 75 else ('Left' if left > right else 'Right'),
    }
    row.update(stat_columns(stats.get(u32(db, p + PLAYER_INDEX))))
    row.update({f'pos_{n}': r for n, r in pos.items() if n != 'SW'})
    row.update({a: round(v / 5) for a, v in attrs.items()})
    return row


def extract(path):
    raw = Path(path).read_bytes()
    entries = read_directory(raw)
    today = game_date(read_entry(raw, entries, 'save_game_summary.dat'))
    print(f'  in-game date: {today}', file=sys.stderr)
    frames = match_frames(raw, entries)  # before the big db: subprocesses spawn faster from a small process
    db = read_entry(raw, entries, 'game_db.dat')
    print(f'  database: {len(db) / 1e6:.0f} MB decompressed', file=sys.stderr)
    names = name_tables(db)
    clubs = club_index(db)
    stats = season_stats(frames, clubs)
    print(f'  season stats: {len(stats)} players with club appearances', file=sys.stderr)

    blocks = list(PLAYER_BLOCK.finditer(db))
    players = []
    for k, block in enumerate(blocks):
        end = block.start() + NAME_SEARCH[1]
        if k + 1 < len(blocks):
            end = min(end, blocks[k + 1].start())
        row = parse_player(db, block, end, names, clubs, stats, today)
        if row:
            players.append(row)
    return players, today


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('save', help='path to the .fm save file')
    ap.add_argument('-o', '--out', help='output CSV (default: exports/<save>_<date>.csv)')
    ap.add_argument('--max-age', type=int, help='only players this age or younger')
    ap.add_argument('--min-pa', type=int, help='only players with at least this potential ability')
    a = ap.parse_args()

    print(f'Reading {a.save}', file=sys.stderr)
    players, today = extract(a.save)
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
    with open(out, 'w', newline='', encoding='utf-8') as f:
        # Players without stats have fewer columns, so take the union in first-seen order.
        w = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for p in players for k in p)))
        w.writeheader()
        w.writerows(players)
    print(f'Wrote {len(players)} players -> {out}', file=sys.stderr)


if __name__ == '__main__':
    main()
