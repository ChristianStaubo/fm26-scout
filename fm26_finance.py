"""Club finances from an FM26 save's game_db.dat. Used by fm26_export.py --clubs.

Finances exist only for clubs in loaded leagues. Each club record (located with
fm26_export.CLUB_RECORD, ending at the next club record) holds:

  * Monthly history: [u32 count][count x (u8 01 + 12 x i32)], one entry per
    completed month since the save started. Fields: MONTH_FIELDS; net ==
    income - expenditure for every club. Balance and wage budget verified
    against the in-game Club > Finances screen.
  * [01][i32 transfer budget][i32 transfer budget remaining] near the start of
    the record (remaining verified in-game), located by searching for the
    latest month's transfer budget - it is missed when the budget changed
    since month end. Sponsorship total (verified) lies 0x1d6 bytes later.

The live balance is not stored as one value (it is the month-end balance plus
this month's income minus expenditure), so the latest month-end balance is
exported with its month.
"""
import struct

import fm26_export as fx

MONTH = 49
MONTH_FIELDS = [
    'balance', 'transfer_budget', 'transfer_remaining', 'wage_budget_pw', 'wage_bill_pw',
    'operating_income', 'transfer_net_paid', 'wages_paid',  # the last two unverified
    'net', 'operating_expenditure', 'income', 'expenditure',
]


def club_records(db):
    """(name, record bytes) for every club record."""
    recs = []
    for m in fx.CLUB_RECORD.finditer(db):
        ln = fx.u32(m.group(6))
        if not 0 < ln < 100:
            continue
        try:
            name = db[m.end():m.end() + ln].decode()
        except UnicodeDecodeError:
            continue
        if name[:1].isalnum():
            recs.append((m.start(), name))
    for (s, name), (t, _) in zip(recs, recs[1:]):
        yield name, db[s:t]


def find_months(b):
    for o in range(4, len(b) - MONTH):
        n = fx.u32(b, o - 4)
        if not 1 <= n <= 60 or o + n * MONTH > len(b):
            continue
        es = []
        for k in range(n):
            p = o + k * MONTH
            if b[p] != 1:
                break
            es.append(struct.unpack_from('<12i', b, p + 1))
        else:
            if all(e[8] == e[10] - e[11] for e in es) and any(e[10] for e in es) and all(e[3] > 0 for e in es):
                return es
    return None


def club_finances(b, today):
    """Finances of one club record, or None. The latest entry is the month before today's."""
    es = find_months(b)
    if not es:
        return None
    months = [dict(zip(MONTH_FIELDS, e)) for e in es]
    last = months[-1]
    year, month = divmod(today.year * 12 + today.month - 2, 12)
    out = {
        'month_end': f'{year}-{month + 1:02d}',
        'month_end_balance': last['balance'],
        'transfer_budget': None, 'transfer_remaining': None,
        'wage_budget_pw': last['wage_budget_pw'], 'month_end_wage_bill_pw': last['wage_bill_pw'],
        'income_12m': sum(m['income'] for m in months[-12:]),
        'expenditure_12m': sum(m['expenditure'] for m in months[-12:]),
    }
    a = b.find(b'\x01' + struct.pack('<i', last['transfer_budget']), 0, 0x300)
    if a >= 0:
        out['transfer_budget'], out['transfer_remaining'] = struct.unpack_from('<ii', b, a + 1)
        out['sponsorship'] = struct.unpack_from('<i', b, a + 1 + 0x1d6)[0]
    return out


def all_finances(db, today):
    res = {}
    for name, b in club_records(db):
        if len(b) > 1500:
            f = club_finances(b, today)
            if f:
                res.setdefault(name, f)
    return res
