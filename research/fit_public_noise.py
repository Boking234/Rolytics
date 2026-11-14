"""Measure day-to-day wobble by game size from public data (the numbers behind priors.py).

Usage:  python research/fit_public_noise.py /path/to/ugc-game-analytics-data
(needs `pip install duckdb`; dataset: github.com/Agustin-Molina/ugc-game-analytics-data)
"""
import json
import sys

import duckdb

R = sys.argv[1]
q=duckdb.sql
q(f"""create table fn as select island_code c, cast(bucket_start as date) d, unique_players u, plays, peak_ccu, avg_minutes_per_player m, retention_d1 r1, retention_d7 r7 from (
 select *, row_number() over (partition by island_code, bucket_start order by snapshot_at desc) rn
 from read_parquet('{R}/fortnite/snapshots/**/*.parquet', hive_partitioning=true, union_by_name=true)
 where interval='day') where rn=1 and not is_partial""")
# day-over-day log changes with consecutive days
q("""create table dd as select a.c, a.d, a.u u0, b.u u1, ln(b.u*1.0/a.u) du,
  case when a.m>0 and b.m>0 then ln(b.m/a.m) end dm,
  b.r1-a.r1 dr1, a.r1 r1, b.r7-a.r7 dr7, a.r7 r7,
  case when a.peak_ccu>0 and b.peak_ccu>0 then ln(b.peak_ccu*1.0/a.peak_ccu) end dp
  from fn a join fn b on a.c=b.c and b.d=a.d+1 where a.u>0 and b.u>0""")
# remove the per-day platform-wide median move (weekday effect), per size bucket
out={}
for name,col in (('players','du'),('minutes','dm'),('peak','dp'),('d1','dr1'),('d7','dr7')):
    rows=q(f"""with x as (select *, cast(floor(log10(greatest(u0,1))*2) as int) b from dd where {col} is not null),
      y as (select *, {col}-median({col}) over (partition by d,b) e from x)
      select b, count(*) n, count(distinct c) islands, median(abs(e))*1.4826/sqrt(2) sigma, median(u0) mu,
        quantile_cont(abs(e),0.99)/sqrt(2) p99
      from y group by b having count(*)>=200 order by b""").fetchall()
    out[name]=rows
    print(name)
    for r in rows:
        print(f'  size~{r[4]:7.0f} n={r[1]:6d} islands={r[2]:5d} sigma={r[3]:.4f}  p99={r[5]:.3f}')
# sigma of retention relative to level and binomial expectation
print(q("""select cast(floor(log10(greatest(u0,1))*2) as int) b, median(r1), median(r7),
  quantile_cont(r1,[0.1,0.25,0.5,0.75,0.9]) q1, quantile_cont(r7,[0.1,0.25,0.5,0.75,0.9]) q7, count(*) from dd where r1 is not null and r7 is not null group by 1 order by 1"""))
# weekday platform-wide effect
print(q("select d, dayname(d), median(du) from dd where u0>=200 group by 1,2 order by 1"))
# how common are big real moves: share of island-days with |du|>ln(1.5) by size
print(q("select cast(floor(log10(greatest(u0,1))*2) as int) b, avg(case when abs(du)>0.405 then 1 else 0 end) big50, avg(case when abs(du)>0.223 then 1 else 0 end) big25, count(*) from dd group by 1 order by 1"))
# Roblox: consecutive-day mean playing change, hot games 11-11 -> 11-12
q(f"""create table rb as select universe_id c, cast(snapshot_at as date) d, max(playing) peak, avg(playing) mean, count(*) n
 from read_parquet('{R}/roblox/snapshots/**/*.parquet', hive_partitioning=true, union_by_name=true) group by 1,2""")
print(q("""with x as (select a.c, a.mean m0, ln(b.mean/a.mean) dm, ln(b.peak*1.0/a.peak) dp from rb a join rb b on a.c=b.c and a.d='2026-11-11' and b.d='2026-11-12' where a.n>=20 and b.n>=20 and a.mean>0 and b.mean>0 and a.peak>0 and b.peak>0),
 y as (select *, cast(floor(log10(m0)*2) as int) b from x), z as (select *, dm-median(dm) over (partition by b) e, dp-median(dp) over (partition by b) ep from y)
 select b, count(*) n, median(m0), median(abs(e))*1.4826/sqrt(2) sigma_mean, median(abs(ep))*1.4826/sqrt(2) sigma_peak, median(dm) from z group by b having count(*)>=30 order by b"""))
print(json.dumps(out)[:0])
