#!/usr/bin/env python3
"""Squad audit + Moneyball + wonderkid shortlists from an fm26_export.py CSV.

Usage:
    python3 analyze.py exports/<save>.csv --club "Brighton & Hove Albion"
    python3 analyze.py exports/<save>.csv --club "Brighton & Hove Albion" --max-wage 40000

How it scores players:
  * Each player is put in a position group from his natural positions.
  * Each group has a handful of key per-90 stats; a player's score is his average
    percentile on those stats among players of the same group in the same
    competition (min. --min-minutes).
  * Squad audit: your regulars' scores and per-stat percentiles.
  * Moneyball: outscores your best player in that group, low wage, age <= --max-age-moneyball.
  * Wonderkids: age <= --max-age-wonderkid, high PA, already playing senior minutes.
"""
import argparse
import bisect
import collections
import csv

GROUPS = {  # group: (natural positions, key stats)
    'CB': ({'DC'}, ['header_pct', 'headers_won_p90', 'tackles_won_p90', 'tackle_pct', 'pass_pct', 'avg_rating']),
    'FB': ({'DL', 'DR', 'WBL', 'WBR'}, ['tackles_won_p90', 'crosses_cmp_p90', 'assists_p90', 'passes_cmp_p90', 'distance_p90', 'avg_rating']),
    'DM': ({'DM'}, ['passes_cmp_p90', 'pass_pct', 'tackles_won_p90', 'tackle_pct', 'distance_p90', 'avg_rating']),
    'CM': ({'MC'}, ['passes_cmp_p90', 'pass_pct', 'assists_p90', 'tackles_won_p90', 'xg_p90', 'avg_rating']),
    'W':  ({'AML', 'AMR', 'ML', 'MR'}, ['goals_p90', 'xg_p90', 'assists_p90', 'shots_p90', 'crosses_cmp_p90', 'avg_rating']),
    'AM': ({'AMC'}, ['goals_p90', 'xg_p90', 'assists_p90', 'passes_cmp_p90', 'shots_p90', 'avg_rating']),
    'ST': ({'ST'}, ['goals_p90', 'xg_p90', 'goals_minus_xg_p90', 'shots_p90', 'headers_won_p90', 'avg_rating']),
}


def num(v, default=None):
    try:
        return float(v) or default
    except (TypeError, ValueError):
        return default


def group_of(row):
    """Most attacking natural position group; None for goalkeepers."""
    natural = set(row['positions'].split('/'))
    if 'GK' in natural:
        return None
    return next((g for g in reversed(GROUPS) if natural & GROUPS[g][0]), None)


def load(path, min_minutes):
    """Outfield players with enough minutes in a known competition."""
    rows = []
    with open(path, encoding='utf-8', newline='') as f:
        for r in csv.DictReader(f):
            r['_min'] = num(r.get('minutes'), 0)
            r['_group'] = group_of(r)
            if r['_min'] >= min_minutes and r['_group'] and r['competition']:
                r['goals_minus_xg_p90'] = num(r['goals_minus_xg'], 0) / (r['_min'] / 90)
                rows.append(r)
    return rows


def add_scores(rows):
    """Percentile of each key stat within (competition, group); score = mean percentile."""
    pools = collections.defaultdict(list)
    for r in rows:
        pools[(r['competition'], r['_group'])].append(r)
    for (_, group), pool in pools.items():
        stats = GROUPS[group][1]
        for s in stats:
            values = [num(r[s], 0) for r in pool]
            ranked = sorted(values)
            for r, v in zip(pool, values):
                r['pct_' + s] = 100 * bisect.bisect_left(ranked, v) / max(1, len(pool) - 1)
        for r in pool:
            r['_score'] = sum(r['pct_' + s] for s in stats) / len(stats)


def fmt_money(w):
    w = num(w)
    return f'£{w / 1000:.0f}K' if w else '-'


def table(rows, cols, title):
    print(f'\n{title}')
    if not rows:
        print('  (none)')
        return
    widths = [max(len(c), *(len(str(f(r))) for r in rows)) for c, f in cols]
    print('  ' + '  '.join(c.ljust(w) for (c, _), w in zip(cols, widths)))
    for r in rows:
        print('  ' + '  '.join(str(f(r)).ljust(w) for (_, f), w in zip(cols, widths)))


BASE_COLS = [('player', lambda r: r['name'])]
MIN_SCORE_COLS = [('min', lambda r: int(r['_min'])), ('score', lambda r: round(r['_score']))]
SHORTLIST_COLS = BASE_COLS + [
    ('age', lambda r: r['age']), ('club', lambda r: r['club'][:24]),
    ('league', lambda r: r['competition'][:20]), ('wage', lambda r: fmt_money(r['wage'])),
] + MIN_SCORE_COLS + [('CA', lambda r: r['ca']), ('PA', lambda r: r['pa'])]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('csv')
    ap.add_argument('--club', required=True, help='your club, exactly as in the CSV')
    ap.add_argument('--league', help="league named in the audit heading (default: your club's)")
    ap.add_argument('--min-minutes', type=int, default=900)
    ap.add_argument('--max-wage', type=int, default=50000, help='Moneyball wage ceiling (per week)')
    ap.add_argument('--max-age-moneyball', type=int, default=28)
    ap.add_argument('--max-age-wonderkid', type=int, default=21)
    ap.add_argument('--min-pa', type=int, default=150, help='wonderkid potential threshold')
    ap.add_argument('--top', type=int, default=8)
    a = ap.parse_args()

    rows = load(a.csv, a.min_minutes)
    add_scores(rows)
    mine = [r for r in rows if r['club'] == a.club]
    if not mine:
        raise SystemExit(f'No players found for club {a.club!r} with {a.min_minutes}+ minutes.')
    league = a.league or collections.Counter(r['competition'] for r in mine).most_common(1)[0][0]

    # 1. Squad audit
    print(f'=== Squad audit: {a.club} vs {league} (percentiles within position, {a.min_minutes}+ min) ===')
    best_score = {}
    for g, (_, stats) in GROUPS.items():
        players = sorted((r for r in mine if r['_group'] == g), key=lambda r: r['_min'], reverse=True)
        if not players:
            continue
        stat_cols = [(s.replace('_p90', '/90'), lambda r, s=s: round(r['pct_' + s])) for s in stats]
        table(players, BASE_COLS + MIN_SCORE_COLS + stat_cols, f'[{g}]')
        best_score[g] = max(r['_score'] for r in players)
    weakest = sorted(best_score, key=best_score.get)[:3]
    print("\nWeakest areas (best regular's score): " + ', '.join(f'{g} ({best_score[g]:.0f})' for g in weakest))

    # 2. Moneyball + 3. Wonderkids for the weakest areas
    for g in weakest:
        # startswith also excludes our own "<club> U21/U18" youth sides
        pool = [r for r in rows if r['_group'] == g and not r['club'].startswith(a.club)]
        money = [r for r in pool if num(r['age'], 99) <= a.max_age_moneyball
                 and 0 < num(r['wage'], 0) <= a.max_wage and r['_score'] >= best_score[g]]
        money.sort(key=lambda r: r['_score'], reverse=True)
        table(money[:a.top], SHORTLIST_COLS,
              f'Moneyball {g}: outscore your best {g} ({best_score[g]:.0f}), wage <= £{a.max_wage // 1000}K, '
              f'age <= {a.max_age_moneyball}')
        kids = [r for r in pool if num(r['age'], 99) <= a.max_age_wonderkid and num(r['pa'], 0) >= a.min_pa]
        kids.sort(key=lambda r: (-num(r['pa'], 0), -r['_score']))
        table(kids[:a.top], SHORTLIST_COLS,
              f'Wonderkids {g}: age <= {a.max_age_wonderkid}, PA >= {a.min_pa}, already playing senior minutes')


if __name__ == '__main__':
    main()
