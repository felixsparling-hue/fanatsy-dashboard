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

s2, swid = os.environ.get("ESPN_S2"), os.environ.get("ESPN_SWID")
if not s2 or not swid:
    sys.exit("Set ESPN_S2 and ESPN_SWID environment variables first.")

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
