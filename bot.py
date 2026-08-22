import os
import json
import re
import time
import requests
import gspread
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from google import genai
from google.genai import errors

# --- TEAM NAME NORMALIZATION & ALIAS MAPPING ---
MLB_TEAM_ALIASES = {
    "arizona diamondbacks": ["arizona diamondbacks", "diamondbacks", "d-backs", "dbacks", "ari", "arizona"],
    "atlanta braves": ["atlanta braves", "braves", "atl", "atlanta"],
    "baltimore orioles": ["baltimore orioles", "orioles", "o's", "os", "bal", "baltimore"],
    "boston red sox": ["boston red sox", "red sox", "bos", "boston"],
    "chicago white sox": ["chicago white sox", "white sox", "cws", "chw", "chicago sox"],
    "chicago cubs": ["chicago cubs", "cubs", "chc", "cubs"],
    "cincinnati reds": ["cincinnati reds", "reds", "cin", "cincinnati"],
    "cleveland guardians": ["cleveland guardians", "guardians", "cle", "cleveland", "indians"],
    "colorado rockies": ["colorado rockies", "rockies", "col", "colorado"],
    "detroit tigers": ["detroit tigers", "tigers", "det", "detroit"],
    "houston astros": ["houston astros", "astros", "hou", "houston"],
    "kansas city royals": ["kansas city royals", "royals", "kc", "kansas city"],
    "los angeles angels": ["los angeles angels", "angels", "laa", "anaheim"],
    "los angeles dodgers": ["los angeles dodgers", "dodgers", "lad", "la dodgers"],
    "miami marlins": ["miami marlins", "marlins", "mia", "miami"],
    "milwaukee brewers": ["milwaukee brewers", "brewers", "mil", "milwaukee"],
    "minnesota twins": ["minnesota twins", "twins", "min", "minnesota"],
    "new york mets": ["new york mets", "mets", "nym", "ny mets"],
    "new york yankees": ["new york yankees", "yankees", "nyy", "ny yankees"],
    "oakland athletics": ["oakland athletics", "athletics", "a's", "as", "oak", "oakland", "sacramento athletics", "las vegas athletics"],
    "philadelphia phillies": ["philadelphia phillies", "phillies", "philly", "phi", "philadelphia"],
    "pittsburgh pirates": ["pittsburgh pirates", "pirates", "bucs", "pit", "pittsburgh"],
    "san diego padres": ["san diego padres", "padres", "sd", "san diego"],
    "san francisco giants": ["san francisco giants", "giants", "sf", "san francisco"],
    "seattle mariners": ["seattle mariners", "mariners", "sea", "seattle"],
    "st. louis cardinals": ["st. louis cardinals", "cardinals", "cards", "stl", "st louis cardinals", "st. louis", "st louis"],
    "tampa bay rays": ["tampa bay rays", "rays", "tb", "tampa bay", "tampa"],
    "texas rangers": ["texas rangers", "rangers", "tex", "texas"],
    "toronto blue jays": ["toronto blue jays", "blue jays", "jays", "tor", "toronto"],
    "washington nationals": ["washington nationals", "nationals", "nats", "wsh", "was", "washington"]
}

def normalize_text(text):
    return re.sub(r'[^a-z0-9]', '', str(text).lower())

def match_canonical_team(name_str):
    if not name_str: return ""
    cleaned = str(name_str).strip().lower()
    cleaned_norm = normalize_text(cleaned)
    for canonical, aliases in MLB_TEAM_ALIASES.items():
        for alias in aliases:
            if alias == cleaned or normalize_text(alias) == cleaned_norm or alias in cleaned or cleaned in alias:
                return canonical.title()
    return name_str.strip().title()

def american_to_decimal(odds):
    try:
        odds_f = float(odds)
        return (odds_f / 100.0) + 1.0 if odds_f > 0 else (100.0 / abs(odds_f)) + 1.0
    except Exception:
        return 1.91

def compute_quarter_kelly_units(odds, model_prob_str):
    try:
        prob_val = float(str(model_prob_str).replace('%', '').strip()) / 100.0
        dec_odds = american_to_decimal(odds)
        kelly = ((dec_odds - 1.0) * prob_val - (1.0 - prob_val)) / (dec_odds - 1.0)
        return max(0.5, min(2.0, round((kelly * 0.25) * 10.0, 2)))
    except Exception:
        return 1.0

def get_sheets():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    service_account_str = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    if not service_account_str: raise ValueError("GCP_SERVICE_ACCOUNT_JSON missing!")
    client = gspread.service_account_from_dict(json.loads(service_account_str), scopes=scopes)
    spreadsheet = client.open("MLB AI Betting Tracker")
    return spreadsheet, spreadsheet.worksheet("MLB")

def ensure_headers(sheet):
    try:
        existing = sheet.get_all_values()
        headers = ["Date", "Pulled Time", "Game", "Bet Type / Sportsbook", "Pick", "Odds", "Implied Prob (%)", "Model Prob (%)", "EV (%)", "Units", "Status", "P/L ($)", "Reasoning", "Validation", "High Agreement & Source Breakdown"]
        if not existing or not existing[0] or existing[0][0] != "Date": sheet.insert_row(headers, index=1)
    except Exception as e: print(f"Header notice: {e}")

def ensure_evolution_sheet(spreadsheet):
    try:
        try: evo_sheet = spreadsheet.worksheet("Evolution & Learnings")
        except Exception: evo_sheet = spreadsheet.add_worksheet(title="Evolution & Learnings", rows=200, cols=10)
        if not evo_sheet.get_all_values():
            evo_sheet.insert_row(["Timestamp", "Sport", "Total Bets Evaluated", "Win Rate (%)", "Net Profit ($)", "Reasoning Factor Weights", "Active Strategy Adjustment", "Validation & Re-Synthesis Notes"], index=1)
        return evo_sheet
    except Exception: return None

def update_evolution_log(spreadsheet, sport_label, memory, summary, time_str):
    try:
        evo_sheet = ensure_evolution_sheet(spreadsheet)
        if evo_sheet:
            evo_sheet.append_row([time_str, sport_label, memory.get("total_bets", 0), memory.get("win_rate", "0%"), memory.get("net_profit_dollars", 0.0), "Standard", memory.get("learnings_and_adjustments", ""), summary])
    except Exception: pass

def fetch_today_probable_pitchers(target_date_str):
    pitcher_map = {}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        espn_resp = requests.get(f"https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard?dates={target_date_str.replace('-', '')}", headers=headers, timeout=10)
        if espn_resp.status_code == 200:
            for event in espn_resp.json().get("events", []):
                comps = event.get("competitions", [])
                if not comps: continue
                competitors = comps[0].get("competitors", [])
                for p in comps[0].get("probables", []):
                    p_name = p.get("athlete", {}).get("displayName", "TBD")
                    team_id = p.get("team", {}).get("id")
                    for c in competitors:
                        if c.get("id") == team_id or c.get("team", {}).get("id") == team_id:
                            pitcher_map[match_canonical_team(c.get("team", {}).get("displayName", ""))] = p_name
    except Exception as e: print(f"Probables notice: {e}")
    return pitcher_map

def fetch_team_high_leverage_hierarchies():
    print("Fetching official MLB season stats for Saves and Holds to identify Closers & Setup Men...")
    high_leverage_map = {}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        teams_resp = requests.get("https://statsapi.mlb.com/api/v1/teams?sportId=1", headers=headers, timeout=10)
        if teams_resp.status_code != 200: return {}
        
        for t in teams_resp.json().get("teams", []):
            team_id = t.get("id")
            canonical_name = match_canonical_team(t.get("name", ""))
            if not canonical_name: continue

            stats_url = f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster?hydrate=person(stats(type=season))"
            stats_resp = requests.get(stats_url, headers=headers, timeout=5)
            if stats_resp.status_code != 200: continue

            closers, setup_men = [], []
            for roster_item in stats_resp.json().get("roster", []):
                person = roster_item.get("person", {})
                p_name = person.get("fullName", "")
                for s in person.get("stats", []):
                    if s.get("type", {}).get("displayName") == "season":
                        split = s.get("splits", [])
                        if split:
                            stat_data = split[0].get("stat", {})
                            saves = int(stat_data.get("saves", 0))
                            holds = int(stat_data.get("holds", 0))
                            if saves >= 2: closers.append((p_name, saves))
                            if holds >= 2: setup_men.append((p_name, holds))

            closers.sort(key=lambda x: x[1], reverse=True)
            setup_men.sort(key=lambda x: x[1], reverse=True)

            high_leverage_map[canonical_name] = {
                "closer": closers[0][0] if closers else "Unknown Closer",
                "setup": [s[0] for s in setup_men[:2]]
            }
    except Exception as e:
        print(f"Notice during season stats ingestion: {e}")
    return high_leverage_map

def fetch_recent_bullpen_usage(days_back=2):
    hl_hierarchy = fetch_team_high_leverage_hierarchies()
    print(f"Analyzing recent box scores using verified season Closer & Setup hierarchies...")
    today = datetime.now(ZoneInfo("America/New_York")).date()
    headers = {"User-Agent": "Mozilla/5.0"}

    team_stats = {}
    for d in range(1, days_back + 1):
        target_date = (today - timedelta(days=d)).strftime("%Y-%m-%d")
        schedule_resp = requests.get(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={target_date}", headers=headers, timeout=10)
        if schedule_resp.status_code != 200: continue
        dates = schedule_resp.json().get("dates", [])
        if not dates: continue

        for game in dates[0].get("games", []):
            if game.get("status", {}).get("abstractGameState") != "Final": continue
            game_pk = game.get("gamePk")
            box_resp = requests.get(f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore", headers=headers, timeout=10)
            if box_resp.status_code != 200: continue
            box_data = box_resp.json()

            for side in ["away", "home"]:
                team_box = box_data.get("teams", {})[side]
                canonical = match_canonical_team(team_box.get("team", {}).get("name", ""))
                if not canonical: continue

                if canonical not in team_stats:
                    team_stats[canonical] = {"raw_pitches": 0, "weighted_load": 0.0, "closer_b2b": False, "setup_b2b": False}

                hierarchy = hl_hierarchy.get(canonical, {"closer": "", "setup": []})
                closer_name = hierarchy.get("closer")
                setup_names = hierarchy.get("setup", [])

                pitchers = team_box.get("pitchers", [])
                players = team_box.get("players", {})

                if len(pitchers) > 1:
                    for pid in pitchers[1:]:
                        p_info = players.get(f"ID{pid}", {})
                        p_name = p_info.get("person", {}).get("fullName", "")
                        p_stats = p_info.get("stats", {}).get("pitching", {})
                        pitches = int(p_stats.get("pitches", p_stats.get("numberOfPitches", 0)))

                        if p_name == closer_name:
                            weight = 3.0
                            team_stats[canonical]["closer_b2b"] = True
                        elif p_name in setup_names:
                            weight = 2.0
                            team_stats[canonical]["setup_b2b"] = True
                        else:
                            weight = 1.0

                        team_stats[canonical]["raw_pitches"] += pitches
                        team_stats[canonical]["weighted_load"] += (pitches * weight)

    objective_ratings = {}
    for team, stats in team_stats.items():
        load = stats["weighted_load"]
        c_b2b = stats["closer_b2b"]
        s_b2b = stats["setup_b2b"]

        if c_b2b or load >= 100:
            status = f"TAXED / FATIGUED (Closer B2B Burn: {c_b2b})"
        elif s_b2b or load >= 60:
            status = "MODERATELY WORKED (Setup Men Used)"
        else:
            status = "FRESH / RESTED (Shutdown Arms Available)"

        objective_ratings[team] = f"Status: {status} | Weighted Backend Load: {round(load, 1)}"

    return objective_ratings

def auto_grade_pending_bets(sheet, odds_key):
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1: return 0
        headers = [h.strip() for h in rows[0]]
        status_idx, game_idx, bet_type_idx, pick_idx, odds_idx, units_idx = headers.index("Status"), headers.index("Game"), headers.index("Bet Type / Sportsbook"), headers.index("Pick"), headers.index("Odds"), headers.index("Units")
        pending = [(i, r) for i, r in enumerate(rows[1:], start=2) if len(r) > status_idx and str(r[status_idx]).strip().upper() == "PENDING"]
        if not pending: return 0

        scores = requests.get(f"https://api.the-odds-api.com/v4/sports/baseball_mlb/scores/?apiKey={odds_key}&daysFrom=3").json()
        updates = []
        for row_idx, r in pending:
            game_title, odds, units = str(r[game_idx]).strip(), float(r[odds_idx]) if r[odds_idx] else -110.0, float(r[units_idx]) if r[units_idx] else 1.0
            for match in scores:
                if not match.get("completed"): continue
                home, away = match.get("home_team", ""), match.get("away_team", "")
                if match_canonical_team(home) in game_title or match_canonical_team(away) in game_title:
                    sc = match.get("scores")
                    if not sc or len(sc) < 2: continue
                    h_score = next((int(s["score"]) for s in sc if s["name"] == home), 0)
                    a_score = next((int(s["score"]) for s in sc if s["name"] == away), 0)
                    status = "WIN" if h_score > a_score else "LOSS"
                    profit = ((100 / abs(odds)) * 100 * units) if (status == "WIN" and odds < 0) else (-100.0 * units)
                    updates.append({"range": f"K{row_idx}:L{row_idx}", "values": [[status, round(profit, 2)]]})
                    break
        if updates: sheet.batch_update(updates)
        return len(updates)
    except Exception as e:
        print(f"Auto-grade notice: {e}")
        return 0

def update_scoreboard(spreadsheet):
    try:
        sb = spreadsheet.worksheet("Scoreboard")
    except Exception:
        sb = spreadsheet.add_worksheet(title="Scoreboard", rows=20, cols=10)
    sb.update(range_name="A1:F4", values=[
        ["Bot / Sport", "Correct Picks (Wins)", "Incorrect Picks (Losses)", "Pending Bets", "Win Rate (%)", "Total Money Won / Lost ($)"],
        ["MLB Bot", '=COUNTIF(MLB!K:K, "WIN")', '=COUNTIF(MLB!K:K, "LOSS")', '=COUNTIF(MLB!K:K, "PENDING")', '=IFERROR(B2/(B2+C2), 0)', '=SUM(MLB!L:L)'],
        ["WNBA Bot", '=COUNTIF(WNBA!K:K, "WIN")', '=COUNTIF(WNBA!K:K, "LOSS")', '=COUNTIF(WNBA!K:K, "PENDING")', '=IFERROR(B3/(B3+C3), 0)', '=SUM(WNBA!L:L)'],
        ["Total Overall", '=B2+B3', '=C2+C3', '=D2+D3', '=IFERROR(B4/(B4+C4), 0)', '=F2+F3']
    ], value_input_option="USER_ENTERED")

def load_memory():
    if os.path.exists("bot_memory.json"):
        try:
            with open("bot_memory.json", "r") as f: return json.load(f)
        except Exception: pass
    return {"total_bets": 0, "wins": 0, "losses": 0, "win_rate": "0%", "net_profit_dollars": 0.0}

def fetch_mlb_odds(odds_key):
    resp = requests.get(f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/?apiKey={odds_key}&regions=us&markets=h2h,spreads,totals&oddsFormat=american")
    return resp.json() if resp.status_code == 200 else []

def get_today_existing_picks(sheet, today_date_str):
    rows = sheet.get_all_values()
    if len(rows) <= 1: return []
    return [{"row_index": i, "game": r[2], "status": r[10]} for i, r in enumerate(rows[1:], start=2) if r[0] == today_date_str and r[10] == "PENDING"]

def format_matchups(odds_data, probable_pitchers, objective_fatigue_ratings):
    valid = []
    for game in odds_data:
        home, away = match_canonical_team(game.get("home_team", "")), match_canonical_team(game.get("away_team", ""))
        if "TBD" in probable_pitchers.get(home, "TBD") or "TBD" in probable_pitchers.get(away, "TBD"): continue
        game_copy = dict(game)
        game_copy["matchup_context"] = {
            "away": f"{away} | Starter: {probable_pitchers.get(away)} | Bullpen: {objective_fatigue_ratings.get(away, 'Fresh')}",
            "home": f"{home} | Starter: {probable_pitchers.get(home)} | Bullpen: {objective_fatigue_ratings.get(home, 'Fresh')}"
        }
        valid.append(game_copy)
    return valid

def generate_picks(odds_data, memory, open_picks, fatigue_ratings, probable_pitchers):
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY")) # <-- FIXED POSITION ARGUMENT ERROR
    formatted_games = format_matchups(odds_data[:8], probable_pitchers, fatigue_ratings)
    if not formatted_games: return {"validations": [], "new_picks": []}

    prompt = f"""
    You are an elite quantitative MLB betting engine.
    Matchups & Season-Weighted Backend Bullpen Status: {json.dumps(formatted_games, indent=2)}
    
    RULES:
    1. Respect the Season-Weighted Bullpen Status. If Python flags a closer on back-to-back usage, heavily penalize that bullpen.
    2. Never swap or hallucinate starting pitchers.
    3. Output STRICT JSON format matching schema:
    {{
      "validations": [],
      "new_picks": [
        {{
          "date": "YYYY-MM-DD",
          "game": "Away @ Home",
          "bet_type": "Moneyline (FanDuel)",
          "pick": "Team",
          "odds": -110,
          "implied_prob": "52.4%",
          "model_prob": "58.0%",
          "expected_value": "+10.7%",
          "high_agreement": "Yes",
          "reasoning": "Reasoning citing season-verified backend bullpen availability and starting pitcher metrics."
        }}
      ]
    }}
    """
    resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    try:
        return json.loads(re.search(r'\{.*\}', resp.text, re.DOTALL).group(0))
    except Exception:
        return {"validations": [], "new_picks": []}

def main():
    spreadsheet, sheet = get_sheets()
    ensure_headers(sheet)
    ensure_evolution_sheet(spreadsheet)
    odds_key = os.environ.get("ODDS_API_KEY")
    if odds_key: auto_grade_pending_bets(sheet, odds_key)
    update_scoreboard(spreadsheet)

    memory = load_memory()
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    
    probable_pitchers = fetch_today_probable_pitchers(today)
    fatigue_data = fetch_recent_bullpen_usage(days_back=2)
    odds = fetch_mlb_odds(odds_key)
    
    if odds:
        ai_resp = generate_picks(odds, memory, get_today_existing_picks(sheet, today), fatigue_data, probable_pitchers)
        for p in ai_resp.get("new_picks", []):
            sheet.append_row([
                p.get("date", today), datetime.now().strftime("%Y-%m-%d %H:%M:%S EDT"),
                p.get("game"), p.get("bet_type"), p.get("pick"), int(p.get("odds")),
                p.get("implied_prob"), p.get("model_prob"), p.get("expected_value"),
                compute_quarter_kelly_units(p.get("odds"), p.get("model_prob")),
                "PENDING", 0.0, p.get("reasoning"), "NEW", p.get("high_agreement")
            ])
    print("Execution complete with Season-Weighted Closer & Setup Men integration!")

if __name__ == "__main__":
    main()
