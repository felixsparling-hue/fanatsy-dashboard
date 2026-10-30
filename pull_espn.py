"""
Phase 1: pull your ESPN fantasy basketball data and save it to raw_espn.json.

Setup (once):
    pip install espn_api

Set your secrets as environment variables (never paste them into a chat or commit them):
    macOS/Linux:  export ESPN_S2="..."  export ESPN_SWID="{...}"
    Windows (PowerShell):  $env:ESPN_S2="..."; $env:ESPN_SWID="{...}"
Keep the curly braces on SWID.

Run:
    python pull_espn.py
"""
import json, os, sys
from espn_api.basketball import League

LEAGUE_ID = 1448219855
TEAM_ID = 1
YEAR = 2027  # ESPN uses the season's ending year (2026-27 season = 2027)



class MissingCookies(Exception):
    pass


def main():
    s2, swid = os.environ.get("ESPN_S2"), os.environ.get("ESPN_SWID")
    if not s2 or not swid:
        raise MissingCookies("ESPN_S2 and ESPN_SWID are not set")

    league = League(league_id=LEAGUE_ID, year=YEAR, espn_s2=s2, swid=swid)


    def player_row(p):
        """Collect what we need from a player, tolerating missing fields."""
        stats = getattr(p, "stats", {}) or {}
        row = {
            "name": getattr(p, "name", None),
            "id": getattr(p, "playerId", None),
            "team": getattr(p, "proTeam", None),
            "position": getattr(p, "position", None),
            "injury": getattr(p, "injuryStatus", None),
            "slot": getattr(p, "lineupSlot", None),
            "stats": {},
        }
        for key, block in stats.items():  # e.g. 2027_total, 2027_last_7, 2027_last_15 ...
            if isinstance(block, dict):
                row["stats"][key] = block.get("avg") or block.get("total")
        return row


    my_team = next(t for t in league.teams if t.team_id == TEAM_ID)
    print(f"League: {league.settings.name}")
    print(f"Your team: {my_team.team_name}")
    print(f"Current matchup week: {league.currentMatchupPeriod}")

    # Matchup totals for the current week
    matchup = None
    try:
        boxes = league.box_scores(
            matchup_period=league.currentMatchupPeriod,
            scoring_period=league.scoringPeriodId,
            matchup_total=True,
        )
        for b in boxes:
            if b.home_team == my_team or b.away_team == my_team:
                mine_home = b.home_team == my_team
                matchup = {
                    "me": my_team.team_name,
                    "opponent": (b.away_team if mine_home else b.home_team).team_name,
                    "my_stats": b.home_stats if mine_home else b.away_stats,
                    "opp_stats": b.away_stats if mine_home else b.home_stats,
                }
                break
    except Exception as e:
        print("Could not read matchup box score:", e)

    opp_team = None
    if matchup:
        opp_team = next(t for t in league.teams if t.team_name == matchup["opponent"])
        print(f"This week's opponent: {matchup['opponent']}")
        for cat, v in matchup["my_stats"].items():
            o = matchup["opp_stats"].get(cat, {})
            print(f"  {cat:5} me={v.get('value')}  opp={o.get('value')}")

    # ESPN's website shows live in-progress scoring; the library's box score can lag behind it.
    # Try several ways of asking ESPN for the scores, print what each returns, and use live totals if found.
    STAT_IDS = {0: "PTS", 1: "BLK", 2: "STL", 3: "AST", 6: "REB", 11: "TO", 13: "FGM", 14: "FGA",
                15: "FTM", 16: "FTA", 17: "3PM", 19: "FG%", 20: "FT%"}
    VARIANTS = {
        "A scoreboard": {"view": ["mMatchupScore", "mScoreboard"], "scoringPeriodId": league.scoringPeriodId},
        "B scoreboard only": {"view": ["mScoreboard"], "scoringPeriodId": league.scoringPeriodId},
        "C everything": {"view": ["mMatchup", "mMatchupScore", "mScoreboard", "mLiveScoring"],
                         "scoringPeriodId": league.scoringPeriodId},
        "D boxscore": {"view": ["mBoxscore", "mMatchupScore"], "scoringPeriodId": league.scoringPeriodId,
                       "matchupPeriodId": league.currentMatchupPeriod},
        "E no period": {"view": ["mMatchupScore", "mScoreboard"]},
    }


    def conv(side, key):
        sbs = (side.get(key) or {}).get("scoreByStat") or {}
        return {STAT_IDS[int(k)]: {"value": v.get("score")} for k, v in sbs.items() if int(k) in STAT_IDS}


    if matchup and opp_team:
        chosen = None
        print("\nTrying ESPN live-score views (compare with your ESPN matchup page):")
        for name, params in VARIANTS.items():
            try:
                raw = league.espn_request.league_get(params=params)
                for m in raw.get("schedule", []):
                    h, a = m.get("home"), m.get("away")
                    if not h or not a or m.get("matchupPeriodId") != league.currentMatchupPeriod:
                        continue
                    sides = {h["teamId"]: h, a["teamId"]: a}
                    if my_team.team_id in sides and opp_team.team_id in sides:
                        for key in ("cumulativeScoreLive", "cumulativeScore"):
                            me_s = conv(sides[my_team.team_id], key)
                            op_s = conv(sides[opp_team.team_id], key)
                            got = "PTS" in me_s and "FGM" in me_s
                            print(f"  {name:18} {key:20} " + (f"PTS me={me_s['PTS']['value']} opp={op_s['PTS']['value']}" if got else "no totals"))
                            if got and key == "cumulativeScoreLive" and not chosen:
                                chosen = (name, me_s, op_s)
                        break
            except Exception as e:
                print(f"  {name:18} failed: {repr(e)[:120]}")
        if chosen:
            print(f"\nUsing LIVE totals from variant: {chosen[0]}")
            matchup["my_stats"], matchup["opp_stats"] = chosen[1], chosen[2]
        else:
            print("\nNo live totals found, so the first numbers above are used.")

    free_agents = league.free_agents(size=250)

    out = {
        "year": YEAR,
        "matchup": matchup,
        "my_roster": [player_row(p) for p in my_team.roster],
        "opp_roster": [player_row(p) for p in opp_team.roster] if opp_team else [],
        "free_agents": [player_row(p) for p in free_agents],
    }
    with open("raw_espn.json", "w") as f:
        json.dump(out, f, indent=2, default=str)

    print(f"\nSaved raw_espn.json  ({len(out['my_roster'])} on your roster, "
          f"{len(out['free_agents'])} free agents)")


def write_error(e):
    text = f"{type(e).__name__}: {e}"
    low = text.lower()
    auth = isinstance(e, MissingCookies) or any(
        w in low for w in ("401", "403", "accessdenied", "access denied", "private", "cookie", "not authorized", "invalid league"))
    err = {"error": {"kind": "auth" if auth else "other", "message": text[:300]}}
    with open("raw_espn.json", "w") as f:
        json.dump(err, f)
    print("\nESPN PULL FAILED:", text[:300])
    print("The site will keep showing the last good data and display a warning.")


try:
    main()
except Exception as e:  # exit normally so the workflow can still publish a warning
    write_error(e)
