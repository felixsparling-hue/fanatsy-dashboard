"""
Streaming engine: picks the best free agent who plays TOMORROW, to swap into your streaming spot.
Reads raw_espn.json (made by pull_espn.py). No cookies needed.

Run:  python3 analyze.py
Optional env vars:  WEEK_END=2026-11-01   TARGET_DATE=2026-10-30
"""
import json, math, os, ssl, datetime as dt, urllib.request

# ---------- SETTINGS YOU CAN EDIT ----------
STREAM_SPOT = ""      # name of the player currently in your streaming spot, e.g. "Kris Dunn".
                      # Leave blank and it will pick the best player to drop each day.
B2B_WEIGHT = 0.5      # how much to value a pickup who also plays the day AFTER tomorrow
                      # (0 = ignore, 1 = count that game fully, since you could keep him)
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
UTIL = 1.0        # daily lineup, empty bench: every healthy rostered player's games count
PRIOR_GP = 20
ALIAS = {"UTAH": "UTA", "GS": "GSW", "SA": "SAS", "NY": "NYK", "NO": "NOP", "NOR": "NOP", "WSH": "WAS",
         "PHO": "PHX", "PHL": "PHI", "BK": "BKN", "BRK": "BKN", "CHO": "CHA"}
NBA30 = set("ATL BOS BKN CHA CHI CLE DAL DEN DET GSW HOU IND LAC LAL MEM MIA MIL MIN NOP NYK OKC ORL PHI PHX POR SAC SAS TOR UTA WAS".split())
norm = lambda t: ALIAS.get((t or "").upper(), (t or "").upper())
phi = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))

data = json.load(open("raw_espn.json"))
if not data.get("matchup"):
    raise SystemExit("No matchup data in raw_espn.json. Re-run pull_espn.py.")

# ---------- dates & schedule ----------
today = dt.date.today()
end = (dt.date.fromisoformat(os.environ["WEEK_END"]) if os.environ.get("WEEK_END")
       else today + dt.timedelta(days=6 - today.weekday()))
target = (dt.date.fromisoformat(os.environ["TARGET_DATE"]) if os.environ.get("TARGET_DATE")
          else today + dt.timedelta(days=1))
target2 = target + dt.timedelta(days=1)
dates = [today + dt.timedelta(days=i) for i in range((end - today).days + 1)]
fetch = [today + dt.timedelta(days=i) for i in range(max((end - today).days, (target2 - today).days) + 1)]
sched, ok = {}, True
for d in fetch:
    try:
        url = ("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates="
               + d.strftime("%Y%m%d"))
        ev = json.load(urllib.request.urlopen(url, timeout=15, context=SSL_CTX)).get("events", [])
        sched[d] = {norm(c["team"]["abbreviation"]) for e in ev
                    for c in e["competitions"][0]["competitors"]}
    except Exception as e:
        print("Schedule fetch failed for", d, "-", e)
        ok = False
if not ok or not any(sched.values()):
    raise SystemExit("Could not load the NBA schedule, so results would be wrong. Stopping.")

plays = lambda team, d: norm(team) in sched.get(d, set())


def team_info(team):
    days = [d for d in dates if plays(team, d)]
    b2b = sum(1 for a, b in zip(days, days[1:]) if (b - a).days == 1)
    return {"games": len(days), "b2b": b2b, "today": today in days}


# ---------- player projections ----------
def per_game(p):
    s = p.get("stats") or {}
    cur = s.get(f"{YEAR}_total")
    prior = next((v for k, v in s.items()
                  if k.endswith("_total") and not k.startswith(YEAR) and v), None)
    gp = (cur or {}).get("GP", 0) or 0
    if cur and prior:
        w = gp / (gp + PRIOR_GP)
        base = {k: w * cur.get(k, 0) + (1 - w) * prior.get(k, 0) for k in KEYS}
    else:
        base = cur if gp else prior
    return {k: float(base.get(k, 0)) for k in KEYS} if base else None


def availability(p):
    inj = (p.get("injury") or "").upper()
    if p.get("slot") == "IR" or inj in ("OUT", "INJURY_RESERVE", "SUSPENSION"):
        return 0.0
    return 0.85 if "DAY" in inj else 1.0


def game_on(p, d):
    """What this player adds in ONE game on date d (zeros if no game / unavailable)."""
    pg = per_game(p)
    if not pg or not plays(p.get("team"), d):
        return {k: 0.0 for k in KEYS}
    return {k: pg[k] * availability(p) * UTIL for k in KEYS}


def rest_of_week(p):
    return {k: sum(game_on(p, d)[k] for d in dates) for k in KEYS}


def projected(base_stats, roster):
    tot = {k: float(base_stats.get(k, {}).get("value", 0) or 0) for k in KEYS}
    games = 0.0
    for p in roster:
        c = rest_of_week(p)
        games += sum(1 for d in dates if plays(p.get("team"), d)) * availability(p) * UTIL
        for k in KEYS:
            tot[k] += c[k]
    return tot, games


def win_probs(me, opp, g_total):
    out, g_total = {}, max(g_total, 1)
    for c in CATS:
        out[c] = phi((me[c] - opp[c]) / (SIGMA[c] * math.sqrt(g_total)))
    for c, m, a in (("FG%", "FGM", "FGA"), ("FT%", "FTM", "FTA")):
        pm, po = me[m] / max(me[a], 1), opp[m] / max(opp[a], 1)
        sd = 1.3 * math.sqrt(pm * (1 - pm) / max(me[a], 1) + po * (1 - po) / max(opp[a], 1))
        out[c] = phi((pm - po) / max(sd, 1e-6))
    return out


m = data["matchup"]
my_roster, opp_roster, fas = data["my_roster"], data["opp_roster"], data["free_agents"]
opp_tot, opp_g = projected(m["opp_stats"], opp_roster)
me_tot, me_g = projected(m["my_stats"], my_roster)
base = win_probs(me_tot, opp_tot, me_g + opp_g)
base_e = sum(base.values())
ALL = CATS + ["FG%", "FT%"]
bad = sorted({p.get("team") for p in my_roster + opp_roster + fas if p.get("team") and norm(p.get("team")) not in NBA30})
if bad:
    print("WARNING: unrecognised team codes (these players will show 0 games):", bad, "\n")

print(f"\n=== {m['me']} vs {m['opponent']} ===")
print(f"Today {today:%a %b %d} | Week ends {end:%a %b %d} | Streaming for {target:%a %b %d}\n")
if target > end:
    print("NOTE: that day is in the NEXT matchup week, so the matchup numbers below don't apply.\n"
          "      Run this again once ESPN has rolled over to the new week.\n")
print("Chance to win each category (projected):")
for c in ALL:
    tag = "swing" if 0.3 <= base[c] <= 0.7 else ("safe" if base[c] > 0.7 else "long shot")
    print(f"  {c:4} {base[c]*100:5.0f}%  {tag}")
print(f"  Expected categories won: {base_e:.2f} of 8\n")

# ---------- streaming spot ----------
streamer = None
if STREAM_SPOT:
    streamer = next((p for p in my_roster if STREAM_SPOT.lower() in p["name"].lower()), None)
    if not streamer:
        print(f"WARNING: couldn't find '{STREAM_SPOT}' on your roster; choosing the best drop automatically.\n")
drops = [streamer] if streamer else [p for p in my_roster if p.get("slot") != "IR"]

# ---------- recommendations ----------
results = []
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
    helps = sorted(ALL, key=lambda c: after[c] - base[c], reverse=True)[:3]
    pg = per_game(fa)
    line = (f"{pg['PTS']:.0f} pts {pg['REB']:.1f} reb {pg['AST']:.1f} ast {pg['STL']:.1f} stl "
            f"{pg['BLK']:.1f} blk {pg['3PM']:.1f} 3pm")
    days = [d.strftime("%a") for d in dates if d >= target and plays(fa["team"], d)]
    results.append({"add": fa["name"], "team": fa["team"], "position": fa["position"],
                    "injury": fa["injury"], "drop": drop["name"], "gain": e - base_e,
                    "games_left": len(days), "days": days, "plays_today": False,
                    "back_to_backs": 1 if (target2 <= end and plays(fa["team"], target2)) else 0,
                    "line": line, "helps": helps})

results.sort(key=lambda r: (r["gain"], r["back_to_backs"]), reverse=True)
print(f"Best streamers for {target:%a %b %d} (add tonight):")
if streamer:
    print(f"(dropping your streaming spot: {streamer['name']})")
for i, r in enumerate(results[:8], 1):
    b2b = "  + plays the next day too" if r["back_to_backs"] else ""
    print(f"{i}. ADD {r['add']} ({r['team']}, {r['position']})  DROP {r['drop']}")
    print(f"   +{r['gain']:.3f} expected cats{b2b}")
    print(f"   {r['line']}   helps: {', '.join(r['helps'])}")

def pack(p):
    pg = per_game(p)
    return {"name": p["name"], "team": p["team"], "pos": p["position"], "inj": p["injury"],
            "slot": p["slot"], "av": availability(p),
            "pg": {k: round(v, 3) for k, v in pg.items()} if pg else None,
            "days": [d.isoformat() for d in fetch if plays(p.get("team"), d)]}


val = lambda st: {k: float(st.get(k, {}).get("value", 0) or 0) for k in KEYS}
engine = {"today": today.isoformat(), "end": end.isoformat(), "target": target.isoformat(),
          "b2b_weight": B2B_WEIGHT, "sigma": SIGMA,
          "cur": {"me": val(m["my_stats"]), "opp": val(m["opp_stats"])},
          "me": [pack(p) for p in my_roster], "opp": [pack(p) for p in opp_roster],
          "fa": [pack(p) for p in fas if per_game(p)]}

json.dump({"engine": engine, "date": str(today), "week_end": str(end), "target_date": str(target),
           "streamer": streamer["name"] if streamer else None,
           "matchup": {"me": m["me"], "opponent": m["opponent"]},
           "win_probs": base, "expected_cats": base_e, "recommendations": results[:10],
           "current": {k: {"me": m["my_stats"].get(k, {}).get("value"),
                           "opp": m["opp_stats"].get(k, {}).get("value")} for k in KEYS + ["FG%", "FT%"]},
           "projected": {"me": me_tot, "opp": opp_tot},
           "roster": [{"name": p["name"], "team": p["team"], "position": p["position"],
                       "injury": p["injury"], "slot": p["slot"],
                       "games": team_info(p["team"])["games"],
                       "today": team_info(p["team"])["today"]} for p in my_roster]},
          open("daily.json", "w"), indent=2, default=str)
print("\nSaved daily.json")
