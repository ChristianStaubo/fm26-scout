"""Full-season league stats from FM26's season-history table.

Used by fm26_export.py. Why: match_stats_cfg.dat (the per-match stat lines the
main export sums) keeps every match of the human manager's club, but for every
other club only about the last 20 matches of the season (roughly February
onwards). Season totals summed from it are therefore partial for most players.
The game also keeps full-season totals, decoded here.

player_stats_hist_dt.cmt: [03 01]["tmc."][u16 version][00], then fixed 152-byte
records, one per (player, club, season, competition). There is no player id in
a record:
    0  u32 club (team index, as in match_stats_cfg.dat)
    4  u16 league id of that club that season (ffff: none)   6  u32 transfer fee (-1 = none)
    10 u16 season start year
    12 u16 sum of match ratings x 10   14 u16 minutes (the game's count; within ~1% of
       the per-match lines, which round substitution times differently)
    23 u8 starts   24 u8 sub appearances   25 u8 appearances (the game's count;
       it leaves out some late cameos)   then counts, see HIST_FIELDS.
    Verified against the summed match lines of the human manager's club over two
    full seasons (every field below exact). For other clubs the per-match lines
    (a part of the season) are contained in the record on every field.
    Keeper records reuse four outfield slots: 43/45 hold penalties faced/saved and
    47/49 saves held/parried instead of tackles and headers.
The records of one club-season sit together, a few per player (one per competition).
A league's records for the season just finished appear when its nation rolls over
to the new season, so right after a season some leagues have none yet.

player_stats.dat: per player [01][u32 player index + 1][4 x u32][00] followed by
stat blocks starting 01 06. The first block is the player's current league
season; once the season is over only appearances (+16) and average rating x 100
(u16, +53) are filled in. Players whose block already holds the new season (a
different, 16-byte header) are not found, so they can't be linked.

Linking a player to his league record (SeasonHistory.link): same club (current
club, then the clubs he played league matches for), the season the per-match
lines cover, same appearances and the same average rating (within rounding). If
the save has per-match lines for him in that league, the record must also
contain them. Seasons still open in the table are skipped (mid-season it only
holds the records of players who left a club). A record two players claim goes
to neither; records claimed are taken out of other players' candidates;
remaining ties stay unlinked.
"""
import collections
import re
import struct

HIST_DT = 'player_stats_hist_dt.cmt'
CURRENT = 'player_stats.dat'
HIST_HEADER = 9
REC_LEN = 152
NO_LEAGUE = 0xffff
YEARS_TRIED = 3

REC_RATING_SUM = 12          # u16, ratings x 10
REC_MINUTES = 14             # u16
REC_STARTS, REC_SUBS, REC_APPS = 23, 24, 25
HIST_FIELDS = {  # name: (offset, struct format, divisor)
    'goals': (26, 'B', 1), 'assists': (27, 'B', 1), 'yellow_cards': (20, 'B', 1),
    'conceded': (16, '<H', 1), 'clean_sheets': (21, 'B', 1),
    'passes_att': (39, '<H', 1), 'passes_cmp': (41, '<H', 1),
    'tackles_att': (43, '<H', 1), 'tackles_won': (45, '<H', 1),
    'headers_att': (47, '<H', 1), 'headers_won': (49, '<H', 1),
    'dribbles': (51, '<H', 1), 'fouls': (53, '<H', 1),
    'shots': (57, '<H', 1), 'shots_on_target': (59, '<H', 1),
    'xg_faced': (69, '<H', 100), 'mistakes_to_goal': (71, '<H', 1),
    'saves_tipped': (81, '<H', 1), 'xgot_faced': (83, '<H', 100), 'key_tackles': (87, '<H', 1),
    'interceptions': (91, '<H', 1), 'sot_faced': (95, '<H', 1),
    'poss_lost': (99, '<H', 1), 'poss_won': (101, '<H', 1),
    'xg': (103, '<H', 100), 'xa': (105, '<H', 100), 'clearances': (107, '<H', 1),
    'progressive_passes': (131, '<H', 1), 'pressures_att': (133, '<H', 1), 'pressures_cmp': (135, '<H', 1),
    'crosses_att': (143, '<H', 1), 'crosses_cmp': (145, '<H', 1), 'shots_blocked': (147, '<H', 1),
}
KEEPER_SLOTS = {'pens_faced': 'tackles_att', 'pens_saved': 'tackles_won',
                'saves_held': 'headers_att', 'saves_parried': 'headers_won'}

REGION = re.compile(rb'\x01(.{4}).{8}\x00{4}.{4}\x00\x01\x06', re.S)
LEAGUE_APPS, LEAGUE_RATING = 2 + 16, 2 + 53   # offsets in the first block (from its 01 06)
RATING_TOLERANCE = 0.5                          # x 100; the history sums ratings rounded to 0.1
MIN_LEAGUE_VOTES, LEAGUE_VOTE_SHARE = 10, 0.6
COMPLETE_SHARE = 0.5   # a league-season with fewer records than this share of its fullest season is still open
CHECK_FIELDS = ('goals', 'assists', 'shots', 'passes_att', 'tackles_att', 'interceptions')
FIT_SHARE, FIT_SLACK = 0.9, 1                   # the two sources occasionally differ by an event or two


def u16(buf, off):
    return struct.unpack_from('<H', buf, off)[0]


def hist_records(data):
    """(club, year) -> [record] for records with at least one appearance, and (club, year) -> its league id."""
    by_club, leagues = collections.defaultdict(list), collections.defaultdict(collections.Counter)
    for o in range(HIST_HEADER, len(data) - REC_LEN + 1, REC_LEN):
        club, league, _, year = struct.unpack_from('<IHIH', data, o)
        if not 1900 < year < 2200 or not data[o + REC_APPS]:
            continue
        by_club[(club, year)].append(data[o:o + REC_LEN])
        if league != NO_LEAGUE:
            leagues[(club, year)][league] += 1
    return by_club, {k: c.most_common(1)[0][0] for k, c in leagues.items()}


def league_summaries(data):
    """Player index -> (league appearances, average rating x 100) of the current league season."""
    found = collections.defaultdict(set)
    for m in REGION.finditer(data):
        p = m.end() - 2
        if p + LEAGUE_RATING + 2 > len(data):
            continue
        apps, rating = data[p + LEAGUE_APPS], u16(data, p + LEAGUE_RATING)
        if apps <= 100 and (rating == 0 or 100 <= rating <= 1000):
            found[struct.unpack('<I', m.group(1))[0] - 1].add((apps, rating))
    return {idx: next(iter(v)) for idx, v in found.items() if len(v) == 1}


def record_totals(rec, keeper=False):
    """Decoded record -> totals in the shape fm26_export.stat_columns expects.

    keeper: the player is a goalkeeper (natural position). His record's tackle and header slots
    hold saves and penalties, so those are None. Outfield records of clubs other than the human
    manager's can hold values in the keeper-only slots, so they can't tell keepers apart."""
    t = {}
    for k, (off, fmt, div) in HIST_FIELDS.items():
        v = struct.unpack_from(fmt, rec, off)[0]
        t[k] = v / div if div > 1 else v
    for k, slot in KEEPER_SLOTS.items():
        t[k] = t[slot] if keeper else 0
    if keeper:
        for slot in KEEPER_SLOTS.values():
            t[slot] = None
    else:
        for k in ('clean_sheets', 'saves_tipped', 'sot_faced', 'xg_faced', 'xgot_faced'):
            t[k] = 0
    apps, minutes = rec[REC_APPS], u16(rec, REC_MINUTES)
    t.update(apps=apps, starts=rec[REC_STARTS], minutes=minutes, rating_sum=u16(rec, REC_RATING_SUM) / 10,
             distance_m=None, gk_apps=apps if keeper else 0, gk_minutes=minutes if keeper else 0)
    return t


def fits(rec, part):
    """Does a full-season record contain the player's partial per-match totals?"""
    t = record_totals(rec)
    return all((t[k] or 0) >= FIT_SHARE * part.get(k, 0) - FIT_SLACK for k in CHECK_FIELDS)


class SeasonHistory:
    """Finds each player's full-season league record."""

    def __init__(self, hist_data, current_data):
        self.records, self.leagues = hist_records(hist_data)
        self.league_size = collections.Counter()  # (league id, year) -> records
        for (club, year), recs in self.records.items():
            if (club, year) in self.leagues:
                self.league_size[(self.leagues[(club, year)], year)] += len(recs)
        self.league_full = collections.Counter()  # league id -> records of its fullest season
        for (league, _), n in self.league_size.items():
            self.league_full[league] = max(self.league_full[league], n)
        self.summaries = league_summaries(current_data)
        self.years = sorted({y for _, y in self.records}, reverse=True)[:YEARS_TRIED]
        self.league_names = {}   # league id -> competition name (see league_names)
        self.window = {}         # (player index, club, year) -> Counter of partial league totals
        self.teams = {}          # player index -> clubs he played league matches for
        self.season_of = None    # club -> the only season to link for it (None: skip the club); None = any recent
        self.links = {}          # player index -> (club, year, record), see link()

    def candidates(self, idx, clubs):
        """(club, year, [records]) that can be the player's league season: same club and season,
        same appearances and average rating, and containing his per-match totals. None if none."""
        apps, rating = self.summaries.get(idx, (0, 0))
        if not apps:
            return None
        for club in dict.fromkeys(c for c in clubs if c is not None):
            years = self.years if self.season_of is None else [self.season_of(club)]
            for year in years:
                if year is None:
                    continue
                cands = [r for r in self.records.get((club, year), ())
                         if r[REC_APPS] == apps
                         and abs(u16(r, REC_RATING_SUM) * 10 / apps - rating) <= RATING_TOLERANCE]
                part = self.window.get((idx, club, year))
                if part:
                    cands = [r for r in cands if fits(r, part)]
                if cands:
                    return club, year, cands
        return None

    def link(self, players):
        """Link players (index -> clubs to try) to their league records; sets self.links.

        A player with one candidate gets it. A record claimed by two such players goes to neither.
        Records claimed are then removed from the other players' candidates until nothing changes;
        players still left with several candidates stay unlinked rather than guess."""
        cands = {idx: c for idx, clubs in players.items() if (c := self.candidates(idx, clubs))}
        links, taken = {}, set()
        while True:
            claims = collections.defaultdict(list)
            for idx, (club, year, recs) in cands.items():
                left = [r for r in recs if id(r) not in taken]
                if len(left) == 1:
                    claims[id(left[0])].append((idx, club, year, left[0]))
            new = {rid: c[0] for rid, c in claims.items() if len(c) == 1}
            for rid, c in claims.items():
                for idx, *_ in c:
                    cands.pop(idx)  # linked, or contested: either way settled
            if not new:
                break
            for rid, (idx, club, year, rec) in new.items():
                links[idx] = club, year, rec
                taken.add(rid)
        self.links = links
        return links

    def find(self, idx, clubs=()):
        """(club, year, record) of the player's league season, or None (after link())."""
        return self.links.get(idx)

    def complete(self, club, year):
        """Is the club's league season closed in the table? Mid-season the table holds only the records
        closed early (players who moved), so those seasons are skipped."""
        league = self.leagues.get((club, year))
        return league is not None and self.league_size[(league, year)] >= COMPLETE_SHARE * self.league_full[league]

    def league_name(self, club, year):
        """Name of the club's league in that season ('' if the save has no matches of it)."""
        return self.league_names.get(self.leagues.get((club, year)), '')


def league_names(leagues, matches):
    """League id -> competition name: the competition its clubs most often play each other in.

    leagues: (club, year) -> league id (SeasonHistory.leagues); matches: (team indices, competition, season).
    Only matches between two clubs of the same league vote, so cups and continental games rarely win.
    """
    votes = collections.defaultdict(collections.Counter)
    for teams, comp, season in matches:
        home, away = (leagues.get((t, season)) for t in teams)
        if comp and comp != 'Friendly' and home is not None and home == away:
            votes[home][comp] += 1
    names = {}
    for league, c in votes.items():
        (comp, n), = c.most_common(1)
        if n >= MIN_LEAGUE_VOTES and n >= LEAGUE_VOTE_SHARE * sum(c.values()):
            names[league] = comp
    return names
