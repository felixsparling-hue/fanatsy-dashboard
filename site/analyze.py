"""
Streaming engine: picks the best free agent who plays TOMORROW, to swap into your streaming spot.
Reads raw_espn.json (made by pull_espn.py). No cookies needed.

Models: recent form, injury status, minutes / teammates-out opportunity, and a modest
opponent matchup factor (pace + points allowed) built from recent NBA results.

Run:  python3 analyze.py
Optional env vars:  WEEK_END=2026-11-01   TARGET_DATE=2026-10-30
"""
import json, math, os, ssl, sys, datetime as dt, urllib.request
from collections import defaultdict

# ---------- SETTINGS YOU CAN EDIT ----------
STREAM_SPOT = ""      # name of the player currently in your streaming spot (blank = best drop each time)
B2B_WEIGHT = 0        # you drop your streamer after one game, so a next-day game earns no credit
FORM_MAX = 0.5        # most weight recent form (last 15 games) can take vs the season blend
FORM_K = 6            # games needed for recent form to reach half of FORM_MAX
OUT_BOOST_PER_MIN = 0.003   # extra production per minute of injured teammates (before the cap)
OUT_BOOST_CAP = 0.08        # never boost a player more than this for teammates being out
HIST_DAYS = 14        # days of past NBA results used for the matchup factor
MF_CAP = 0.07         # matchup factor never moves a projection more than +/- 7%
# -------------------------------------------

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CTX = None

YEAR = "2027"
CATS = ["PTS", "REB", "AST", "STL", "BLK", "3PM"]
KEYS = CATS + ["FGM", "FGA", "FTM", "FTA"]
SIGMA = {"PTS": 7.0, "REB": 3.0, "AST": 2.5, "STL": 1.0, "BLK": 1.0, "3PM": 1.5}
UTIL = 1.0            # daily lineup, empty bench: every healthy rostered player's games count
PRIOR_GP = 20
ALIAS = {"UTAH": "UTA", "GS": "GSW", "SA": "SAS", "NY": "NYK", "NO": "NOP", "NOR": "NOP", "WSH": "WAS",
         "PHO": "PHX", "PHL": "PHI", "BK": "BKN", "BRK": "BKN", "CHO": "CHA"}
NBA30 = set("ATL BOS BKN CHA CHI CLE DAL DEN DET GSW HOU IND LAC LAL MEM MIA MIL MIN NOP NYK OKC ORL PHI PHX POR SAC SAS TOR UTA WAS".split())
norm = lambda t: ALIAS.get((t or "").upper(), (t or "").upper())
phi = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
mean = lambda v: sum(v) / len(v)

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/Toronto")
except Exception:
    TZ = None
now_utc = dt.datetime.now(dt.timezone.utc)
today = (now_utc.astimezone(TZ) if TZ else now_utc - dt.timedelta(hours=4)).date()

data = json.load(open("raw_espn.json"))

# ---------- if the ESPN pull failed, keep the last good data and attach a warning ----------
if data.get("error"):
    prev = {}
    try:
        prev = json.load(open("prev.json"))
        if not isinstance(prev, dict):
            prev = {}
    except Exception:
        pass
    prev["error"] = {**data["error"], "at": now_utc.isoformat()}
    json.dump(prev, open("daily.json", "w"), indent=2)
    print("ESPN pull failed:", data["error"].get("message"))
    print("Wrote daily.json with a warning" + (" (kept the last good data)." if prev.get("engine") else " (no earlier data available)."))
    sys.exit(0)
if not data.get("matchup"):
    raise SystemExit("No matchup data in raw_espn.json. Re-run pull_espn.py.")

# ---------- dates & schedule ----------
end = (dt.date.fromisoformat(os.environ["WEEK_END"]) if os.environ.get("WEEK_END")
       else today + dt.timedelta(days=6 - today.weekday()))
target = (dt.date.fromisoformat(os.environ["TARGET_DATE"]) if os.environ.get("TARGET_DATE")
          else today + dt.timedelta(days=1))
target2 = target + dt.timedelta(days=1)
dates = [today + dt.timedelta(days=i) for i in range((end - today).days + 1)]
fetch = [today + dt.timedelta(days=i) for i in range(max((end - today).days, (target2 - today).days) + 1)]

sched, slate, seen, started = {}, {}, set(), set()
opp_of = {}                      # (team, date) -> opponent
results = []                     # completed games: (team, opp, points for, points against)
fetch_failed = False
first = today - dt.timedelta(days=HIST_DAYS + 1)
last = fetch[-1] + dt.timedelta(days=1)
d = first
while d <= last:
    try:
        url = ("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates="
               + d.strftime("%Y%m%d"))
        ev = json.load(urllib.request.urlopen(url, timeout=15, context=SSL_CTX)).get("events", [])
        for e in ev:
            if e["id"] in seen:
                continue
            seen.add(e["id"])
            # bucket each game by its own Eastern-time date, whatever day ESPN listed it under
            t = dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
            gd = t.astimezone(TZ).date() if TZ else (t - dt.timedelta(hours=4)).date()
            comp = e["competitions"][0]["competitors"]
            teams = [norm(c["team"]["abbreviation"]) for c in comp]
            sched.setdefault(gd, set()).update(teams)
            if len(teams) == 2:
                opp_of[(teams[0], gd)], opp_of[(teams[1], gd)] = teams[1], teams[0]
            if gd == today and t <= now_utc:
                started.update(teams)   # already tipped off: the live totals include this game
            slate.setdefault(gd, []).append("-".join(sorted(teams)))
            if e.get("status", {}).get("type", {}).get("completed") and len(comp) == 2:
                try:
                    sc = {norm(c["team"]["abbreviation"]): float(c["score"]) for c in comp}
                    a, b = teams
                    results.append((a, b, sc[a], sc[b]))
                    results.append((b, a, sc[b], sc[a]))
                except Exception:
                    pass
    except Exception as ex:
        if d >= today - dt.timedelta(days=1):      # missing history is tolerable; missing future is not
            print("Schedule fetch failed for", d, "-", ex)
            fetch_failed = True
    d += dt.timedelta(days=1)
sched[today] = sched.get(today, set()) - started   # don't count today's games twice
if started:
    print(f"Today's games already started (counted in live totals, not projected): {sorted(started)}")
if fetch_failed or not any(sched.get(x) for x in fetch):
    raise SystemExit("Could not load the NBA schedule, so results would be wrong. Stopping.")

plays = lambda team, day: norm(team) in sched.get(day, set())


def team_info(team):
    days = [x for x in dates if plays(team, x)]
    b2b = sum(1 for a, b in zip(days, days[1:]) if (b - a).days == 1)
    return {"games": len(days), "b2b": b2b, "today": today in days}


# ---------- matchup factor: opponent defence + pace, shrunk hard toward 1 ----------
pf, pa = defaultdict(list), defaultdict(list)
for t_, o_, a_, b_ in results:
    pf[t_].append(a_)
    pa[t_].append(b_)
league_pts = mean([r[2] for r in results]) if results else None


def shrunk(vals, league, k=6):
    if not vals or not league:
        return 1.0
    return 1 + (mean(vals) / league - 1) * len(vals) / (len(vals) + k)


def_ratio = {t_: shrunk(pa[t_], league_pts) for t_ in pa}                      # >1 = allows many points
tot_ratio = {t_: shrunk([x + y for x, y in zip(pf[t_], pa[t_])], 2 * league_pts if league_pts else None) for t_ in pf}


def matchup_factor(team, day):
    t_ = norm(team)
    o_ = opp_of.get((t_, day))
    if not o_ or not league_pts:
        return 1.0
    pace = (tot_ratio.get(t_, 1.0) + tot_ratio.get(o_, 1.0)) / 2
    f = 1 + 0.6 * (def_ratio.get(o_, 1.0) - 1) + 0.4 * (pace - 1)
    return max(1 - MF_CAP, min(1 + MF_CAP, f))


# ---------- player model: season blend + recent form, injuries, minutes ----------
def availability(p):
    inj = (p.get("injury") or "").upper()
    if p.get("slot") == "IR" or inj in ("OUT", "INJURY_RESERVE", "SUSPENSION"):
        return 0.0
    if inj == "DOUBTFUL":
        return 0.25
    if inj == "QUESTIONABLE":
        return 0.6
    if "DAY" in inj:
        return 0.8
    if inj == "PROBABLE":
        return 0.95
    return 1.0


comp_score = lambda g: g["PTS"] + 1.2 * g["REB"] + 1.5 * g["AST"] + 3 * g["STL"] + 3 * g["BLK"] + g["3PM"]
mpg_of = lambda b: float((b or {}).get("MPG") or (b or {}).get("MIN") or 0)


def build(p):
    s = p.get("stats") or {}
    cur = s.get(f"{YEAR}_total")
    prior = next((v for k, v in s.items() if k.endswith("_total") and not k.startswith(YEAR) and v), None)
    gp = (cur or {}).get("GP", 0) or 0
    if cur and prior:
        w = gp / (gp + PRIOR_GP)
        base = {k: w * float(cur.get(k, 0) or 0) + (1 - w) * float(prior.get(k, 0) or 0) for k in KEYS}
    elif cur and gp:
        base = {k: float(cur.get(k, 0) or 0) for k in KEYS}
    elif prior:
        base = {k: float(prior.get(k, 0) or 0) for k in KEYS}
    else:
        return {"pg": None, "mpg": 0.0, "tags": []}
    tags, mpg = [], mpg_of(cur) or mpg_of(prior)
    rec = s.get(f"{YEAR}_last_15") or s.get(f"{YEAR}_last_30")
    grp = (rec or {}).get("GP", 0) or 0
    if rec and grp >= 3 and grp < gp:          # only when it's a genuine subset of the season
        wr = FORM_MAX * grp / (grp + FORM_K)
        rd = {k: float(rec.get(k, 0) or 0) for k in KEYS}
        before = comp_score(base)
        base = {k: (1 - wr) * base[k] + wr * rd[k] for k in KEYS}
        if before > 0:
            ch = comp_score(rd) / before - 1
            if ch >= 0.12:
                tags.append(f"hot form (+{ch*100:.0f}%)")
            elif ch <= -0.12:
                tags.append(f"cold form ({ch*100:.0f}%)")
        mr, ms = mpg_of(rec), mpg_of(cur)
        if mr and ms:
            mpg = mr
            if mr - ms >= 3 and mr / ms >= 1.1:
                tags.append(f"minutes up ({mr:.0f} vs {ms:.0f})")
            elif ms - mr >= 3 and mr / ms <= 0.9:
                tags.append(f"minutes down ({mr:.0f} vs {ms:.0f})")
    if 0 < mpg < 16:
        tags.append(f"low minutes ({mpg:.0f} mpg)")
    return {"pg": base, "mpg": mpg, "tags": tags}


m = data["matchup"]
my_roster, opp_roster, fas = data["my_roster"], data["opp_roster"], data["free_agents"]
ALLP = my_roster + opp_roster + fas
for p in ALLP:
    p["_m"] = build(p)

# opportunity: minutes vacated by injured teammates (only counts players we can see)
out_min = defaultdict(float)
for p in ALLP:
    if availability(p) <= 0.25:
        out_min[norm(p.get("team"))] += p["_m"]["mpg"]
for p in ALLP:
    mm = p["_m"]
    if not mm["pg"] or availability(p) <= 0.25 or mm["mpg"] < 12:
        continue
    boost = min(OUT_BOOST_CAP, OUT_BOOST_PER_MIN * out_min.get(norm(p.get("team")), 0.0))
    if boost >= 0.01:
        mm["pg"] = {k: v * (1 + boost) for k, v in mm["pg"].items()}
        if boost >= 0.02:
            mm["tags"].append(f"teammates out (+{boost*100:.0f}%)")

per_game = lambda p: p["_m"]["pg"]


def game_on(p, day):
    """What this player adds in ONE game on that date (zeros if no game / unavailable)."""
    pg = per_game(p)
    if not pg or not plays(p.get("team"), day):
        return {k: 0.0 for k in KEYS}
    f = availability(p) * UTIL * matchup_factor(p.get("team"), day)
    return {k: pg[k] * f for k in KEYS}


def rest_of_week(p):
    out = {k: 0.0 for k in KEYS}
    for day in dates:
        g = game_on(p, day)
        for k in KEYS:
            out[k] += g[k]
    return out


def projected(base_stats, roster):
    tot = {k: float(base_stats.get(k, {}).get("value", 0) or 0) for k in KEYS}
    games = 0.0
    for p in roster:
        c = rest_of_week(p)
        games += sum(1 for x in dates if plays(p.get("team"), x)) * availability(p) * UTIL
        for k in KEYS:
            tot[k] += c[k]
    return tot, games


def win_probs(me, opp, g_total):
    out, g_total = {}, max(g_total, 1)
    for c in CATS:
        out[c] = phi((me[c] - opp[c]) / (SIGMA[c] * math.sqrt(g_total)))
    for c, mk, at in (("FG%", "FGM", "FGA"), ("FT%", "FTM", "FTA")):
        pm, po = me[mk] / max(me[at], 1), opp[mk] / max(opp[at], 1)
        sd = 1.3 * math.sqrt(pm * (1 - pm) / max(me[at], 1) + po * (1 - po) / max(opp[at], 1))
        out[c] = phi((pm - po) / max(sd, 1e-6))
    return out


opp_tot, opp_g = projected(m["opp_stats"], opp_roster)
me_tot, me_g = projected(m["my_stats"], my_roster)
base_p = win_probs(me_tot, opp_tot, me_g + opp_g)
base_e = sum(base_p.values())
ALL = CATS + ["FG%", "FT%"]
bad = sorted({p.get("team") for p in ALLP if p.get("team") and norm(p.get("team")) not in NBA30})
if bad:
    print("WARNING: unrecognised team codes (these players will show 0 games):", bad, "\n")

print(f"\n=== {m['me']} vs {m['opponent']} ===")
print(f"Today {today:%a %b %d} | Week ends {end:%a %b %d} | Streaming for {target:%a %b %d}")
print(f"Matchup factors built from {len(results)//2} completed NBA games; recent-form weight up to {FORM_MAX:.0%}.\n")
if target > end:
    print("NOTE: that day is in the NEXT matchup week, so the matchup numbers below don't apply.\n"
          "      Run this again once ESPN has rolled over to the new week.\n")
print(f"NBA games on {target:%a %b %d} ({len(slate.get(target, []))}): " + ", ".join(sorted(slate.get(target, []))) + "\n")
print("Chance to win each category (projected):")
for c in ALL:
    tag = "swing" if 0.3 <= base_p[c] <= 0.7 else ("safe" if base_p[c] > 0.7 else "long shot")
    print(f"  {c:4} {base_p[c]*100:5.0f}%  {tag}")
print(f"  Expected categories won: {base_e:.2f} of 8\n")

# ---------- streaming spot ----------
streamer = None
if STREAM_SPOT:
    streamer = next((p for p in my_roster if STREAM_SPOT.lower() in p["name"].lower()), None)
    if not streamer:
        print(f"WARNING: couldn't find '{STREAM_SPOT}' on your roster; choosing the best drop automatically.\n")
drops = [streamer] if streamer else [p for p in my_roster if p.get("slot") != "IR"]

# ---------- recommendations ----------
recs = []
for fa in fas:
    if availability(fa) == 0 or not per_game(fa) or not plays(fa.get("team"), target):
        continue
    n1 = game_on(fa, target)
    n2 = game_on(fa, target2) if target2 <= end else {k: 0.0 for k in KEYS}
    best = None
    for drop in drops:
        d1 = game_on(drop, target)
        d2 = game_on(drop, target2) if target2 <= end else {k: 0.0 for k in KEYS}
        t = {k: me_tot[k] + (n1[k] - d1[k]) + B2B_WEIGHT * (n2[k] - d2[k]) for k in KEYS}
        e = sum(win_probs(t, opp_tot, me_g + opp_g).values())
        if best is None or e > best[0]:
            best = (e, drop, t)
    e, drop, t = best
    after = win_probs(t, opp_tot, me_g + opp_g)
    helps = sorted(ALL, key=lambda c: after[c] - base_p[c], reverse=True)[:3]
    pg = per_game(fa)
    line = (f"{pg['PTS']:.0f} pts {pg['REB']:.1f} reb {pg['AST']:.1f} ast {pg['STL']:.1f} stl "
            f"{pg['BLK']:.1f} blk {pg['3PM']:.1f} 3pm")
    days = [x.strftime("%a") for x in dates if x >= target and plays(fa["team"], x)]
    recs.append({"add": fa["name"], "team": fa["team"], "position": fa["position"],
                 "injury": fa["injury"], "drop": drop["name"], "gain": e - base_e,
                 "games_left": len(days), "days": days, "plays_today": False,
                 "back_to_backs": 1 if (target2 <= end and plays(fa["team"], target2)) else 0,
                 "line": line, "helps": helps, "tags": fa["_m"]["tags"]})

recs.sort(key=lambda r: (r["gain"], r["back_to_backs"]), reverse=True)
print(f"Best streamers for {target:%a %b %d} (add tonight):")
if streamer:
    print(f"(dropping your streaming spot: {streamer['name']})")
for i, r in enumerate(recs[:8], 1):
    print(f"{i}. ADD {r['add']} ({r['team']}, {r['position']})  DROP {r['drop']}")
    print(f"   {r['gain']:+.3f} expected cats   {r['line']}   helps: {', '.join(r['helps'])}")
    if r["tags"]:
        print(f"   notes: {', '.join(r['tags'])}")


def pack(p):
    mm = p["_m"]
    return {"name": p["name"], "team": p["team"], "pos": p["position"], "inj": p["injury"],
            "slot": p["slot"], "av": availability(p), "mpg": round(mm["mpg"], 1), "tags": mm["tags"],
            "pg": {k: round(v, 3) for k, v in mm["pg"].items()} if mm["pg"] else None,
            "days": [x.isoformat() for x in fetch if plays(p.get("team"), x)],
            "mf": {x.isoformat(): round(matchup_factor(p.get("team"), x), 3)
                   for x in fetch if plays(p.get("team"), x)}}


val = lambda st: {k: float(st.get(k, {}).get("value", 0) or 0) for k in KEYS}
engine = {"today": today.isoformat(), "end": end.isoformat(), "target": target.isoformat(),
          "b2b_weight": B2B_WEIGHT, "sigma": SIGMA,
          "cur": {"me": val(m["my_stats"]), "opp": val(m["opp_stats"])},
          "me": [pack(p) for p in my_roster], "opp": [pack(p) for p in opp_roster],
          "fa": [pack(p) for p in fas if per_game(p)]}

now_local = now_utc.astimezone(TZ) if TZ else now_utc - dt.timedelta(hours=4)
json.dump({"engine": engine, "generated_at": now_local.strftime("%a %b %d, %I:%M %p ET"),
           "generated_iso": now_utc.isoformat(), "date": str(today), "week_end": str(end),
           "target_date": str(target),
           "slate": {str(k): sorted(v) for k, v in slate.items() if today <= k <= target2},
           "streamer": streamer["name"] if streamer else None,
           "matchup": {"me": m["me"], "opponent": m["opponent"]},
           "win_probs": base_p, "expected_cats": base_e, "recommendations": recs[:10],
           "current": {k: {"me": m["my_stats"].get(k, {}).get("value"),
                           "opp": m["opp_stats"].get(k, {}).get("value")} for k in KEYS + ["FG%", "FT%"]},
           "projected": {"me": me_tot, "opp": opp_tot},
           "roster": [{"name": p["name"], "team": p["team"], "position": p["position"],
                       "injury": p["injury"], "slot": p["slot"],
                       "games": team_info(p["team"])["games"],
                       "today": team_info(p["team"])["today"]} for p in my_roster]},
          open("daily.json", "w"), indent=2, default=str)
print("\nSaved daily.json")
