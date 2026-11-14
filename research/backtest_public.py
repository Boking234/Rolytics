"""Check the "first weeks" mode against real public data.

Usage:  python research/backtest_public.py /path/to/ugc-game-analytics-data
(needs `pip install duckdb`; the dataset is github.com/Agustin-Molina/ugc-game-analytics-data)

For every Fortnite Creative island with 10 complete public days, the first 8
days are treated as the game's whole history and the last 2 days are checked,
once with the old flat guess for normal wobble and once with the size-based
guess learned from public data. Each day's platform-wide move (the median
change of islands of similar size, e.g. a Fortnite-wide quiet Sunday) is
removed first: the real tool learns such weekday patterns after three weeks,
which 10 days of data cannot show. Most islands then had an ordinary two
days, so the share flagged should be small and not depend on island size.
"""
import math
import sys
from datetime import date
from statistics import median

import duckdb

from roblox_agent import catalog, detect

root = sys.argv[1]
detect.WINDOW = 2
rows = duckdb.sql(f"""
 with x as (select island_code c, cast(bucket_start as date) d, unique_players u, peak_ccu p,
     avg_minutes_per_player m, retention_d1 r1,
     row_number() over (partition by island_code, bucket_start order by snapshot_at desc) rn
   from read_parquet('{root}/fortnite/snapshots/**/*.parquet', hive_partitioning=true, union_by_name=true)
   where interval='day' and not is_partial)
 select c, d, u, p, m, r1 from x where rn=1 and c in
   (select c from x where rn=1 group by c having count(*)>=10 and median(u)>=20) order by c, d""").fetchall()
games: dict[str, list] = {}
for c, d, u, p, m, r1 in rows:
    games.setdefault(c, []).append((d, u, p, m, r1))
METRICS = (("DailyActiveUsers", 1), ("PeakConcurrentPlayers", 2), ("AverageSessionLengthMinutes", 3),
           ("ForwardD1Retention", 4))
BUCKETS = ((20, 100), (100, 1000), (1000, 10 ** 9))
print(f"{len(games)} islands with 10 complete days")


def bucket(size):
    return next(i for i, (lo, hi) in enumerate(BUCKETS) if lo <= size < hi)


sizes = {c: sorted(r[1] for r in g)[len(g) // 2] for c, g in games.items()}
# platform-wide level of each day, per size bucket and metric: median log value relative to the island's own median
platform = {}
for col in (1, 2, 3, 4):
    acc = {}
    for c, g in games.items():
        vals = [r[col] for r in g if r[col]]
        if len(vals) < 10:
            continue
        mid = median(vals)
        for r in g:
            if r[col]:
                acc.setdefault((bucket(sizes[c]), r[0]), []).append(math.log(r[col] / mid))
    platform[col] = {k: median(v) for k, v in acc.items()}
print("Typical surprise of the last day (0.67 = the guess for normal wobble is exactly right; "
      "higher = too jumpy, lower = too sleepy), and share of islands with a very big move:")
for metric, col in METRICS:
    spec = catalog.get(metric)
    for lo, hi in BUCKETS:
        zf, zi = [], []
        for g in games.values():
            size = sorted(r[1] for r in g)[len(g) // 2]
            if not lo <= size < hi:
                continue
            pts = [detect.Point(r[0] if isinstance(r[0], date) else r[0].date(),
                                float(r[col]) / math.exp(platform[col].get((bucket(size), r[0]), 0.0)), "Valid")
                   for r in g if r[col] is not None and r[col] > 0]
            if len(pts) < 10:
                continue
            zf.append(abs(detect._baseline(spec, pts, pts[-1:], None, None)[1][0]))
            zi.append(abs(detect._baseline(spec, pts, pts[-1:], None, size)[1][0]))
        if len(zf) >= 10:
            def big(z):
                return 100 * sum(1 for v in z if v >= 4) / len(z)
            print(f"{spec.label:26s} {lo:>5}-{hi if hi < 10**9 else 'up':<6} islands={len(zf):4d}  "
                  f"flat guess {median(zf):4.2f} ({big(zf):4.1f}% big)   size-based {median(zi):4.2f} ({big(zi):4.1f}% big)")
