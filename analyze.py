"""
Phase 2+3: schedule layer + recommendation engine.
Reads raw_espn.json (made by pull_espn.py), fetches the NBA schedule for the rest of
your matchup week, and recommends waiver pickups. No cookies needed.

Run:  python3 analyze.py
Optional: set WEEK_END=2026-11-01 if your matchup week doesn't end on Sunday.
"""
import json, math, os, ssl, datetime as dt, urllib.request

try:  # fixes "CERTIFICATE_VERIFY_FAILED" on many Macs
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CTX = None

YEAR = "2027"
CATS = ["PTS", "REB", "AST", "STL", "BLK", "3PM"]
KEYS = CATS + ["FGM", "FGA", "FTM", "FTA"]
SIGMA = {"PTS": 7.0, "REB": 3.0, "AST": 2.5, "STL": 1.0, "BLK": 1.0, "3PM": 1.5}  # per player-game
UTIL = 1.0        # daily lineup, empty bench: every healthy rostered player's games count
PRIOR_GP = 20     # how strongly last season's numbers anchor early-season stats
ALIAS = {"UTAH": "UTA", "GS": "GSW", "SA": "SAS", "NY": "NYK", "NO": "NOP", "WSH": "WAS", "PHO": "PHX"}

norm = lambda t: ALIAS.get((t or "").upper(), (t or "").upper())
phi = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))

data = json.load(open("raw_espn.json"))
if not data.get("matchup"):
    raise SystemExit("No matchup data in raw_espn.json. Re-run pull_espn.py.")

# ---------- schedule ----------
today = dt.date.today()
end = (dt.date.fromisoformat(os.environ["WEEK_END"]) if os.environ.get("WEEK_END")
       else today + dt.timedelta(days=6 - today.weekday()))
dates = [today + dt.timedelta(days=i) for i in range((end - today).days + 1)]
sched, ok = {}, True
for d in dates:
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
    raise SystemExit("Could not load the NBA schedule, so results would be wrong. "
                     "Stopping. Paste the error above into the chat.")


def team_info(team):
    days = [d for d in dates if norm(team) in sched.get(d, set())]
    b2b = sum(1 for a, b in zip(days, days[1:]) if (b - a).days == 1)
    return {"games": len(days), "b2b": b2b, "today": today in days,
            "days": [d.strftime("%a") for d in days]}


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


def contribution(p):
    pg = per_game(p)
    if not pg:
        return {k: 0.0 for k in KEYS}, 0.0
    g = team_info(p.get("team"))["games"] * availability(p) * UTIL
    return {k: pg[k] * g for k in KEYS}, g


def projected(base_stats, roster, skip=None, add=None):
    tot = {k: float(base_stats.get(k, {}).get("value", 0) or 0) for k in KEYS}
    games = 0.0
    for p in list(roster) + ([add] if add else []):
        if p is skip:
            continue
        c, g = contribution(p)
        games += g
        for k in KEYS:
            tot[k] += c[k]
    return tot, games


def win_probs(me, opp, g_total):
    out = {}
    g_total = max(g_total, 1)
    for c in CATS:
        sd = SIGMA[c] * math.sqrt(g_total)
        out[c] = phi((me[c] - opp[c]) / sd)
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

print(f"\n=== {m['me']} vs {m['opponent']} ===")
print(f"Week ends {end.strftime('%a %b %d')}. Days left incl. today: {len(dates)}\n")
print("Chance to win each category (projected):")
for c in CATS + ["FG%", "FT%"]:
    tag = "swing" if 0.3 <= base[c] <= 0.7 else ("safe" if base[c] > 0.7 else "long shot")
    print(f"  {c:4} {base[c]*100:5.0f}%  {tag}")
print(f"  Expected categories won: {base_e:.2f} of 8\n")

# ---------- recommendations ----------
results = []
for fa in fas:
    if availability(fa) == 0 or not per_game(fa):
        continue
    best = None
    for drop in my_roster:
        t, g = projected(m["my_stats"], my_roster, skip=drop, add=fa)
        e = sum(win_probs(t, opp_tot, g + opp_g).values())
        if best is None or e > best[0]:
            best = (e, drop, t, g)
    e, drop, t, g = best
    after = win_probs(t, opp_tot, g + opp_g)
    helps = sorted(CATS + ["FG%", "FT%"], key=lambda c: after[c] - base[c], reverse=True)[:3]
    ti = team_info(fa["team"])
    results.append({"add": fa["name"], "team": fa["team"], "position": fa["position"],
                    "injury": fa["injury"], "drop": drop["name"], "gain": e - base_e,
                    "games_left": ti["games"], "back_to_backs": ti["b2b"],
                    "plays_today": ti["today"], "days": ti["days"], "helps": helps})

results.sort(key=lambda r: (r["gain"], r["games_left"]), reverse=True)
print("Top pickups for today:")
for i, r in enumerate(results[:5], 1):
    flags = []
    if r["plays_today"]:
        flags.append("plays today")
    if r["back_to_backs"]:
        flags.append(f"{r['back_to_backs']} back-to-back")
    print(f"{i}. ADD {r['add']} ({r['team']}, {r['position']})  DROP {r['drop']}")
    print(f"   +{r['gain']:.2f} expected cats | {r['games_left']} games left "
          f"({', '.join(r['days'])}) {'| ' + ', '.join(flags) if flags else ''}")
    print(f"   helps most: {', '.join(r['helps'])}")

json.dump({"date": str(today), "week_end": str(end), "matchup": {"me": m["me"], "opponent": m["opponent"]},
           "win_probs": base, "expected_cats": base_e, "recommendations": results[:10],
           "current": {k: {"me": m["my_stats"].get(k, {}).get("value"),
                           "opp": m["opp_stats"].get(k, {}).get("value")}
                       for k in KEYS + ["FG%", "FT%"]},
           "projected": {"me": me_tot, "opp": opp_tot},
           "roster": [{"name": p["name"], "team": p["team"], "position": p["position"],
                       "injury": p["injury"], "slot": p["slot"],
                       "games": team_info(p["team"])["games"],
                       "today": team_info(p["team"])["today"]} for p in my_roster]},
          open("daily.json", "w"), indent=2, default=str)
print("\nSaved daily.json")
