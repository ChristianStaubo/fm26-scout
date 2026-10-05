"""Contract, loan, squad status and nationality fields for FM26 game_db.dat players.

Used by fm26_export.py. Offsets are relative to a
player's data block start `p` (PLAYER_BLOCK match start) and his name record `q`
(as returned by fm26_export.find_name).

Contract structure (several may sit between p and q, in this order per contract):
    ... ff*8 00 00 00 [u8 n] n x [u32 value][u16 param][u16 type]   bonus/clause list
    ... [u32 ?][u32 -1][u32 team | -1]                              team = U21/U18 team index
        [u16 day|time<<9][u16 year] expiry   [date] start (1900 = none)
        [u8 squad status][00][u8 squad status][u8 flags] [u16][u32 n29]
        n29 x 29-byte records (history; not decoded)
        ... 18+11 bytes ...
    [u16 club][6 x 00][u32 weekly wage][01 xx 00]                   wage record
The main block is located by alignment: wage_record - (expiry_offset + 18) is a
multiple of 29.

Kinds of contract record for one player:
    * permanent contract: has a start date (marker byte usually 0x0b; 0x00 for
      deals done this window).
    * loan: a second dated contract with a different club; its expiry is the
      loan end and its wage the wage paid at the loan club.
    * undated contracts are not active: same club = agreed renewal (the game
      shows its expiry already), other club = offer from another club.
"""
import datetime as dt
import re
import struct

NONE32 = 0xffffffff
WAGE_RECORD = re.compile(rb'(?=(.{2})\x00{6}(.{4})\x01(.)\x00)', re.S)
MAIN_BLOCK = re.compile(rb'(?<=\xff\xff\xff\xff....)(?=(..[\xd0-\xff]\x07)(..[\x6c-\xff]\x07))', re.S)
CLAUSE_LIST = re.compile(rb'\xff{8}\x00\x00\x00([\x01-\x10])', re.S)
RECORD_29 = 29
LOOKBACK = 1500

# Squad playing-time status (validated against in-game squad screen, Brighton 2026-06).
SQUAD_STATUS = {
    2: 'Important Player', 3: 'Regular Starter', 4: 'Squad Player', 7: 'Fringe Player',
    10: 'Breakthrough Prospect', 15: 'First-Choice Goalkeeper', 20: 'Backup',
    # 7 matches the finance screen's Fringe Player wage total; unverified codes seen: 9, 11, 13 (youth), 0
}

# Bonus types in the clause list (value in GBP, -1 = n/a). Names are a hypothesis
# from which players carry them (keepers have no 0x21; 0x26 is always smallest).
BONUS_TYPES = {0x20: 'appearance_fee', 0x21: 'goal_bonus', 0x22: 'clean_sheet_bonus',
               0x26: 'unused_sub_fee', 0x27: 'assist_bonus'}

# Nation index -> name. Indices are save-specific to FM26's database, identified
# from the clubs registered in each nation; 16 Brighton players validated in-game.
NATIONS = {
    0: 'Algeria', 1: 'Angola', 2: 'Benin', 4: 'Burkina Faso', 6: 'Cameroon', 7: 'Cape Verde',
    8: 'Central African Republic', 11: 'Egypt', 12: 'Equatorial Guinea', 14: 'Gabon', 15: 'Gambia',
    16: 'Ghana', 17: 'Guinea', 18: 'Guinea-Bissau', 19: 'Ivory Coast', 20: 'Kenya', 24: 'Madagascar',
    26: 'Mali', 27: 'Mauritania', 29: 'Morocco', 33: 'Nigeria', 36: 'Senegal', 38: 'Sierra Leone',
    40: 'South Africa', 43: 'Tanzania', 44: 'Congo', 45: 'Togo', 46: 'Tunisia', 47: 'Uganda',
    48: 'DR Congo', 50: 'Zimbabwe', 58: 'Indonesia', 59: 'Iran', 60: 'Iraq', 61: 'Japan', 62: 'Jordan',
    64: 'Kazakhstan', 66: 'Kyrgyzstan', 77: 'Qatar', 78: 'Saudi Arabia', 80: 'South Korea', 82: 'Syria',
    85: 'Thailand', 88: 'United Arab Emirates', 89: 'Uzbekistan', 97: 'Canada', 99: 'Costa Rica',
    103: 'Antigua and Barbuda', 105: 'Guyana', 106: 'Haiti', 107: 'Honduras', 108: 'Jamaica',
    109: 'Mexico', 110: 'Curaçao', 112: 'Panama', 115: 'St Kitts and Nevis', 117: 'Suriname',
    119: 'Trinidad and Tobago', 120: 'United States', 126: 'Albania', 128: 'Armenia', 129: 'Austria',
    131: 'Belgium', 133: 'Bosnia and Herzegovina', 134: 'Bulgaria', 135: 'Croatia', 136: 'Cyprus',
    137: 'Czech Republic', 138: 'Denmark', 139: 'England', 142: 'Finland', 143: 'France',
    144: 'Georgia', 145: 'Germany', 146: 'Greece', 147: 'Hungary', 148: 'Iceland', 149: 'Israel',
    150: 'Italy', 154: 'Luxembourg', 155: 'North Macedonia', 156: 'Malta', 157: 'Moldova',
    158: 'Netherlands', 159: 'Northern Ireland', 160: 'Norway', 161: 'Poland', 162: 'Portugal',
    163: 'Republic of Ireland', 164: 'Romania', 165: 'Russia', 167: 'Scotland', 168: 'Slovakia',
    169: 'Slovenia', 170: 'Spain', 171: 'Sweden', 172: 'Switzerland', 173: 'Turkey', 174: 'Ukraine',
    175: 'Wales', 176: 'Serbia', 177: 'Australia', 180: 'New Zealand', 187: 'Argentina', 189: 'Brazil',
    190: 'Chile', 191: 'Colombia', 192: 'Ecuador', 193: 'Paraguay', 194: 'Peru', 195: 'Uruguay',
    196: 'Venezuela', 216: 'Gibraltar', 219: 'Kosovo', 225: 'French Guiana', 226: 'Guadeloupe',
    227: 'Martinique', 229: 'Saint-Martin', 234: 'Comoros', 247: 'Montenegro',
}


def _u32(buf, off):
    return struct.unpack_from('<I', buf, off)[0]


def _date(buf, off):
    v, year = struct.unpack_from('<HH', buf, off)
    if year <= 1900:
        return None
    return dt.date(year, 1, 1) + dt.timedelta(((v & 0x1ff) or 1) - 1)


def _clauses(db, main):
    """Bonus/clause list preceding a contract main block -> (bonuses, other clauses)."""
    found = None
    for m in CLAUSE_LIST.finditer(db, max(0, main - 300), main):
        found = m
    if not found:
        return {}, []
    bonuses, other = {}, []
    o = found.end()
    for _ in range(found.group(1)[0]):
        value, param, kind = struct.unpack_from('<IHH', db, o)
        o += 8
        if param == 0xffff and kind in BONUS_TYPES:
            bonuses[BONUS_TYPES[kind]] = None if value == NONE32 else value
        else:
            other.append({'type': kind, 'param': param, 'value': None if value == NONE32 else value})
    return bonuses, other


def player_contracts(db, p, q):
    """Every contract record between a player's block (p) and name record (q)."""
    out = []
    for m in WAGE_RECORD.finditer(db, p, q):
        c = m.start()
        main = None
        for dm in MAIN_BLOCK.finditer(db, max(p, c - LOOKBACK), c):
            if (c - (dm.start() + 18)) % RECORD_29 == 0:
                main = dm.start()
        if main is None:
            continue
        wage = _u32(db, c + 8)
        if wage > 10_000_000:  # false positive
            continue
        team = _u32(db, main - 4)
        bonuses, other = _clauses(db, main)
        out.append({
            'club': struct.unpack_from('<H', db, c)[0],
            'wage': wage,
            'marker': m.group(3)[0],
            'expires': _date(db, main),
            'started': _date(db, main + 4),
            'squad_status_code': db[main + 8],
            'team': None if team == NONE32 else team,
            'bonuses': bonuses,
            'clauses': other,
        })
    return out


def _same_club(a, b, clubs):
    """True when team indices a and b are the same club (U21/U18 teams count as their parent)."""
    if a == b:
        return True
    na, nb = clubs.get(a, ''), clubs.get(b, '')
    return bool(na and nb) and (na.startswith(nb + ' ') or nb.startswith(na + ' '))


def contract_status(db, p, q, current_club, clubs):
    """Summarise a player's contract situation.

    current_club: u16 at p + PLAYER_CLUB (the team he currently plays for - the loan
    club while on loan). Returns a dict of plain values (clubs as names).
    """
    cs = player_contracts(db, p, q)
    dated = [c for c in cs if c['started']]
    if not dated:
        return {'contract_club': None}
    # Permanent contract: marker 0x0b if present, else the earliest start date.
    parent = max(dated, key=lambda c: (c['marker'] == 0x0b, -c['started'].toordinal()))
    loan = next((c for c in dated if c is not parent and not _same_club(c['club'], parent['club'], clubs)), None)
    renewal = next((c for c in cs if not c['started'] and _same_club(c['club'], parent['club'], clubs)
                    and c['expires'] and c['expires'] > parent['expires']), None)
    offers = [c for c in cs if not c['started'] and not _same_club(c['club'], parent['club'], clubs)]
    active = renewal or parent
    name = lambda i: clubs.get(i, f'club#{i}')
    return {
        'contract_club': name(parent['club']),
        'current_team': name(current_club),
        'wage': active['wage'],  # an agreed renewal's wage is what the game shows
        'contract_expires': active['expires'].isoformat(),
        'joined_club': parent['started'].isoformat(),
        'squad_status': SQUAD_STATUS.get(parent['squad_status_code'], f'code {parent["squad_status_code"]}'),
        'renewal_wage': renewal['wage'] if renewal else None,
        'renewal_expires': renewal['expires'].isoformat() if renewal else None,
        'on_loan_at': name(loan['club']) if loan else None,
        'loan_ends': loan['expires'].isoformat() if loan else None,
        'loan_wage': loan['wage'] if loan else None,
        'contract_offers': '; '.join(f"{name(o['club'])} £{o['wage']}/w to {o['expires']}" for o in offers) or None,
        **{k: v for k, v in parent['bonuses'].items()},
    }


def nationality(db, q):
    """Nation index and name; q is the name record offset. Index is a u32 9 bytes after the dob."""
    full_len = _u32(db, q + 16)
    idx = _u32(db, q + 20 + full_len + 4 + 9)
    if idx > 400:
        return None, None
    return idx, NATIONS.get(idx, f'nation#{idx}')
