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
        b = dec_odds - 1.0
        if b <= 0: return 1.0
        kelly = (b * prob_val - (1.0 - prob_val)) / b
        if kelly <= 0: return 0.5
        raw_units = (kelly * 0.25) * 40.0
        return max(0.5, min(3.0, round(raw_units, 2)))
    except Exception:
        return 1.0

# --- 1. GOOGLE SHEETS SETUP ---
def get_sheets():
    print("Connecting to Google Sheets (MLB Tab)...")
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    service_account_str = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    if not service_account_str: raise ValueError("GCP_SERVICE_ACCOUNT_JSON missing!")
    client = gspread.service_account_from_dict(json.loads(service_account_str), scopes=scopes)
    spreadsheet = client.open("MLB AI Betting Tracker")
    return spreadsheet, spreadsheet.worksheet("MLB")

def ensure_headers(sheet):
    try:
        existing = sheet.get_all_values()
        headers = [
            "Date", "Pulled Time", "Game", "Bet Type / Sportsbook", "Pick", "Odds", 
            "Implied Prob (%)", "Model Prob (%)", "EV (%)", "Units", "Status", "P/L ($)", 
            "Reasoning", "Validation", "High Agreement & Source Breakdown", "Game Start Time",
            "Away Bullpen Math", "Home Bullpen Math", "Python Math Baseline", "AI Contextual Shift"
        ]
        if not existing or not existing[0] or existing[0][0] != "Date": 
            sheet.insert_row(headers, index=1)
    except Exception as e: 
        print(f"Header notice: {e}")

def ensure_evolution_sheet(spreadsheet):
    try:
        try: evo_sheet = spreadsheet.worksheet("Evolution & Learnings")
        except Exception: evo_sheet = spreadsheet.add_worksheet(title="Evolution & Learnings", rows=200, cols=10)
        if not evo_sheet.get_all_values():
            evo_sheet.insert_row(["Timestamp", "Sport", "Total Bets Evaluated", "Win Rate (%)", "Net Profit ($)", "Reasoning Factor Weights", "Active Strategy Adjustment", "Validation & Re-Synthesis Notes"], index=1)
        return evo_sheet
    except Exception: return None

# --- 2. ADVANCED METRICS & PROBABLES ---
def fetch_team_advanced_metrics():
    metrics_map = {}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        teams_url = "https://statsapi.mlb.com/api/v1/teams?sportId=1"
        teams_resp = requests.get(teams_url, headers=headers, timeout=10)
        if teams_resp.status_code == 200:
            for t in teams_resp.json().get("teams", []):
                t_id = t.get("id")
                canonical = match_canonical_team(t.get("name", ""))
                if not canonical: continue
                
                metrics_map[canonical] = {"ops": 0.720, "whip": 1.30}
                stats_url = f"https://statsapi.mlb.com/api/v1/teams/{t_id}/stats?stats=season&group=hitting,pitching"
                stats_resp = requests.get(stats_url, headers=headers, timeout=5)
                
                if stats_resp.status_code == 200:
                    for stat_group in stats_resp.json().get("stats", []):
                        group_name = stat_group.get("group", {}).get("displayName")
                        splits = stat_group.get("splits", [])
                        if not splits: continue
                        stat_data = splits[0].get("stat", {})
                        if group_name == "hitting":
                            metrics_map[canonical]["ops"] = float(stat_data.get("ops", ".720"))
                        elif group_name == "pitching":
                            metrics_map[canonical]["whip"] = float(stat_data.get("whip", "1.30"))
    except Exception: pass
    return metrics_map

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
                            team_name = match_canonical_team(c.get("team", {}).get("displayName", ""))
                            if team_name: pitcher_map[team_name] = p_name
    except Exception: pass
    return pitcher_map

# --- 3. FULL-HIERARCHY BULLPEN & SELF-LEARNING MATH ENGINE ---
def load_memory():
    if os.path.exists("bot_memory.json"):
        try:
            with open("bot_memory.json", "r") as f: return json.load(f)
        except Exception: pass
    return {
        "total_bets": 0, "wins": 0, "losses": 0,
        "reasoning_factor_weights": {
            "bullpen_depth_and_fatigue": {"weight": 1.0},
            "platoon_and_lineup_splits": {"weight": 1.0},
            "starting_pitcher_expected_metrics": {"weight": 1.0}
        }
    }

def fetch_team_high_leverage_hierarchies():
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
                        for split in s.get("splits", []):
                            stat_data = split.get("stat", {})
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
    except Exception: pass
    return high_leverage_map

def fetch_recent_bullpen_usage(days_back=2):
    hl_hierarchy = fetch_team_high_leverage_hierarchies()
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
                    team_stats[canonical] = {
                        "closer_dates": set(), "setup_dates": set(),
                        "closer_pitches": 0, "setup_pitches": 0, "middle_pitches": 0
                    }

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
                            team_stats[canonical]["closer_dates"].add(target_date)
                            team_stats[canonical]["closer_pitches"] += pitches
                        elif p_name in setup_names:
                            team_stats[canonical]["setup_dates"].add(target_date)
                            team_stats[canonical]["setup_pitches"] += pitches
                        else:
                            team_stats[canonical]["middle_pitches"] += pitches

    objective_ratings = {}
    for team, stats in team_stats.items():
        cp, sp, mp = stats["closer_pitches"], stats["setup_pitches"], stats["middle_pitches"]
        load = (cp * 3.0) + (sp * 2.0) + (mp * 1.0)
        c_b2b = len(stats["closer_dates"]) >= 2
        s_b2b = len(stats["setup_dates"]) >= 2

        if c_b2b or load >= 120: status = f"TAXED (Closer B2B: {c_b2b})"
        elif s_b2b or load >= 70: status = "MODERATELY WORKED"
        else: status = "FRESH"

        breakdown = f"(Closer: {cp}x3) + (Setup: {sp}x2) + (Middle: {mp}x1)"
        objective_ratings[team] = {
            "status_string": f"Status: {status} | Load: {round(load, 1)} | Math: {breakdown}",
            "load": load,
            "closer_b2b": c_b2b
        }
    return objective_ratings

def calculate_strict_baseline(away, home, fatigue_data, advanced_metrics, memory):
    home_prob = 0.50 
    math_log = []
    weights = memory.get("reasoning_factor_weights", {})
    
    bp_weight = weights.get("bullpen_depth_and_fatigue", {}).get("weight", 1.0)
    ops_weight = weights.get("platoon_and_lineup_splits", {}).get("weight", 1.0)
    whip_weight = weights.get("starting_pitcher_expected_metrics", {}).get("weight", 1.0)
    
    a_load = fatigue_data.get(away, {}).get("load", 0)
    h_load = fatigue_data.get(home, {}).get("load", 0)
    bp_shift = (((a_load - h_load) / 100.0) * 0.015) * bp_weight
    home_prob += bp_shift
    math_log.append(f"Bullpen Load (wt {bp_weight}x): {round(bp_shift*100, 2)}%")
    
    b2b_shift = 0.0
    if fatigue_data.get(away, {}).get("closer_b2b"): b2b_shift += (0.02 * bp_weight)
    if fatigue_data.get(home, {}).get("closer_b2b"): b2b_shift -= (0.02 * bp_weight)
    home_prob += b2b_shift
    
    a_ops = advanced_metrics.get(away, {}).get("ops", 0.720)
    h_ops = advanced_metrics.get(home, {}).get("ops", 0.720)
    ops_shift = (((h_ops - a_ops) / 0.050) * 0.02) * ops_weight
    home_prob += ops_shift
    math_log.append(f"OPS Shift (wt {ops_weight}x): {round(ops_shift*100, 2)}%")
    
    a_whip = advanced_metrics.get(away, {}).get("whip", 1.30)
    h_whip = advanced_metrics.get(home, {}).get("whip", 1.30)
    whip_shift = (((a_whip - h_whip) / 0.10) * 0.015) * whip_weight
    home_prob += whip_shift
    math_log.append(f"WHIP Shift (wt {whip_weight}x): {round(whip_shift*100, 2)}%")
    
    home_prob += 0.015
    math_log.append("HFA: +1.50%")
    
    home_prob = max(0.05, min(0.95, home_prob))
    return home_prob, 1.0 - home_prob, " | ".join(math_log)

# --- 4. AUTO-GRADING & ANTI-DUPLICATION ---
def auto_grade_pending_bets(sheet, odds_key):
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1: return 0
        headers = [h.strip() for h in rows[0]]
        status_idx = headers.index("Status")
        game_idx = headers.index("Game")
        pick_idx = headers.index("Pick")
        odds_idx = headers.index("Odds")
        units_idx = headers.index("Units")
        
        pending_rows = [(i, r) for i, r in enumerate(rows[1:], start=2) if len(r) > status_idx and str(r[status_idx]).strip().upper() == "PENDING"]
        if not pending_rows: return 0

        scores_url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/scores/?apiKey={odds_key}&daysFrom=3"
        resp = requests.get(scores_url)
        if resp.status_code != 200: return 0
        scores_data = resp.json()
        updates = []

        for row_idx, r in pending_rows:
            pick_date_str = str(r[0]).strip()
            game_title = str(r[game_idx]).strip()
            pick_str = str(r[pick_idx]).strip()
            try: odds = float(r[odds_idx])
            except: odds = -110.0
            try: units = float(r[units_idx]) if r[units_idx] else 1.0
            except: units = 1.0

            for match in scores_data:
                if not match.get("completed"): continue
                commence_time_str = match.get("commence_time", "")
                match_date_ny_str = ""
                if commence_time_str:
                    try:
                        game_dt_utc = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
                        match_date_ny_str = game_dt_utc.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
                    except Exception: pass
                
                if pick_date_str != match_date_ny_str: continue
                home_team = match.get("home_team", "")
                away_team = match.get("away_team", "")
                if match_canonical_team(home_team) in game_title or match_canonical_team(away_team) in game_title:
                    scores = match.get("scores")
                    if not scores or len(scores) < 2: continue
                    home_score = next((int(s["score"]) for s in scores if s["name"] == home_team), 0)
                    away_score = next((int(s["score"]) for s in scores if s["name"] == away_team), 0)
                    
                    winner = home_team if home_score > away_score else away_team
                    status = "WIN" if match_canonical_team(pick_str).lower() == match_canonical_team(winner).lower() else "LOSS"
                    profit = ((odds / 100.0) * 100.0 * units) if (status == "WIN" and odds > 0) else ((100.0 / abs(odds)) * 100.0 * units) if status == "WIN" else (-100.0 * units)
                    updates.append({"range": f"K{row_idx}:L{row_idx}", "values": [[status, round(profit, 2)]]})
                    break
        if updates: sheet.batch_update(updates)
    except Exception: pass

def update_scoreboard(spreadsheet):
    try:
        try: sb = spreadsheet.worksheet("Scoreboard")
        except: sb = spreadsheet.add_worksheet(title="Scoreboard", rows=20, cols=10)
        scoreboard_data = [
            ["Bot / Sport & Timeframe", "Correct Picks (Wins)", "Incorrect Picks (Losses)", "Pending Bets", "Win Rate (%)", "Total Money Won / Lost ($)"],
            ["MLB Bot (All-Time)", '=COUNTIF(MLB!K:K, "WIN")', '=COUNTIF(MLB!K:K, "LOSS")', '=COUNTIF(MLB!K:K, "PENDING")', '=IFERROR(B2/(B2+C2), 0)', '=SUM(MLB!L:L)']
        ]
        sb.clear()
        sb.update(range_name="A1:F2", values=scoreboard_data, value_input_option="USER_ENTERED")
    except Exception: pass

def get_today_existing_picks(sheet, today_date_str):
    rows = sheet.get_all_values()
    if len(rows) <= 1: return []
    existing = []
    for r in rows[1:]:
        if len(r) > 10 and str(r[0]).strip() == today_date_str and str(r[10]).strip().upper() == "PENDING":
            existing.append(str(r[2]).strip())
    return existing

# --- 5. MATCHUP FORMATTING ---
def fetch_mlb_odds(odds_key):
    resp = requests.get(f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/?apiKey={odds_key}&regions=us&markets=h2h,spreads,totals&oddsFormat=american")
    return resp.json() if resp.status_code == 200 else []

def format_matchups(odds_data, probable_pitchers, objective_fatigue_ratings, advanced_metrics, memory):
    valid = []
    matchup_cache = {}  
    current_utc = datetime.now(ZoneInfo("UTC"))
    
    for game in odds_data:
        home, away = match_canonical_team(game.get("home_team", "")), match_canonical_team(game.get("away_team", ""))
        commence_time_str = game.get("commence_time")
        game_time_et = "Unknown Time"
        if commence_time_str:
            try:
                dt_utc = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
                if dt_utc < current_utc: continue
                game_time_et = dt_utc.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %I:%M %p EDT")
            except Exception: pass

        h_pitcher = probable_pitchers.get(home, "TBD")
        a_pitcher = probable_pitchers.get(away, "TBD")
        
        home_prob, away_prob, math_log = calculate_strict_baseline(away, home, objective_fatigue_ratings, advanced_metrics, memory)
        
        away_bp_str = objective_fatigue_ratings.get(away, {}).get('status_string', 'Status: FRESH | Math: N/A')
        home_bp_str = objective_fatigue_ratings.get(home, {}).get('status_string', 'Status: FRESH | Math: N/A')
        
        game_key = f"{away} @ {home}"
        matchup_cache[game_key] = {"away_bp": away_bp_str, "home_bp": home_bp_str, "math": math_log}

        game_copy = dict(game)
        game_copy["matchup_context"] = {
            "start_time": game_time_et,
            "away": f"{away} | Starter: {a_pitcher} | Bullpen: {away_bp_str} | OPS: {advanced_metrics.get(away, {}).get('ops', 0.720)} | WHIP: {advanced_metrics.get(away, {}).get('whip', 1.30)}",
            "home": f"{home} | Starter: {h_pitcher} | Bullpen: {home_bp_str} | OPS: {advanced_metrics.get(home, {}).get('ops', 0.720)} | WHIP: {advanced_metrics.get(home, {}).get('whip', 1.30)}"
        }
        game_copy["python_math_baseline"] = {
            "away_win_prob_baseline": f"{round(away_prob * 100, 1)}%",
            "home_win_prob_baseline": f"{round(home_prob * 100, 1)}%",
            "calculation_log": math_log
        }
        valid.append(game_copy)
    return valid, matchup_cache

# --- 6. AI SYNTHESIS WITH MEMORY FEEDBACK ---
def parse_json_from_response(response):
    raw_text = getattr(response, "text", "")
    if hasattr(response, "candidates") and response.candidates:
        raw_text = "".join([p.text for p in response.candidates[0].content.parts if hasattr(p, "text") and p.text])
    json_match = re.search(r'\{.*\}', raw_text.strip(), re.DOTALL)
    if json_match:
        try: return json.loads(json_match.group(0))
        except Exception: pass
    return json.loads(raw_text.replace("```json", "").replace("```", "").strip())

def generate_picks_and_validations(formatted_games, memory):
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    prompt = f"""
    You are an elite Bounded Contextual Adjuster. Python has calculated strict baselines using active factor memory weights: {json.dumps(memory.get('reasoning_factor_weights', {}))}

    === TODAY'S MATCHUPS & BASELINES ===
    {json.dumps(formatted_games, indent=2)}

    STRICT RULES:
    1. THE TIGHT LEASH (±5.0% MAX): Adjust baselines by a maximum of ± 5.0%.
    2. ANTI-HYPE & OBJECTIVITY: Disregard media hype. Base synthesis on hard data.
    3. THE 11 PERCENT EV THRESHOLD: Recommend picks where final `model_prob` gives an EV of 11.0% or higher.

    OUTPUT SCHEMA (STRICT JSON):
    {{
      "new_picks": [
        {{
          "date": "YYYY-MM-DD",
          "start_time": "YYYY-MM-DD HH:MM PM EDT",
          "game": "Away Team @ Home Team",
          "bet_type": "Moneyline (FanDuel)",
          "pick": "Team Name",
          "odds": -110,
          "implied_prob": "52.4%",
          "model_prob": "55.0%",
          "expected_value": "+11.7%",
          "high_agreement": "Consensus",
          "reasoning": "Data-driven explanation",
          "ai_contextual_shift": "Shifted +X% because strict factual reason"
        }}
      ]
    }}
    """
    for model_name in ["gemini-3.1-pro-preview", "gemini-3.7-flash"]:
        for _ in range(2):
            try:
                response = client.models.generate_content(model=model_name, contents=prompt)
                return parse_json_from_response(response)
            except Exception: time.sleep(5)
    return {"new_picks": []}

# --- 7. MAIN EXECUTION ---
def main():
    spreadsheet, sheet = get_sheets()
    ensure_headers(sheet)
    ensure_evolution_sheet(spreadsheet)
    odds_key = os.environ.get("ODDS_API_KEY")
    auto_grade_pending_bets(sheet, odds_key)
    update_scoreboard(spreadsheet)
    
    memory = load_memory()
    today_date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    current_time_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S EDT")
    
    probable_pitchers = fetch_today_probable_pitchers(today_date_str)
    advanced_metrics = fetch_team_advanced_metrics()
    fatigue_data = fetch_recent_bullpen_usage(days_back=2)
    odds = fetch_mlb_odds(odds_key)
    if not odds: return

    formatted_games, matchup_cache = format_matchups(odds, probable_pitchers, fatigue_data, advanced_metrics, memory)
    if not formatted_games: return

    existing_games = get_today_existing_picks(sheet, today_date_str)
    ai_response = generate_picks_and_validations(formatted_games, memory)
    new_picks = ai_response.get("new_picks", [])
    appended = 0
    
    for p in new_picks:
        game = str(p.get("game", "")).strip()
        if game in existing_games:
            print(f"  [Duplicate Blocked] Skipping {game} (Already logged as PENDING).")
            continue

        pick_date = str(p.get("date", today_date_str)).strip()
        model_prob_str = str(p.get("model_prob", "50.0%"))
        try: odds_val = float(p.get("odds", -110))
        except: odds_val = -110.0

        qk_units = compute_quarter_kelly_units(odds_val, model_prob_str)
        cache_data = matchup_cache.get(game, {})

        sheet.append_row([
            pick_date, current_time_str, game, str(p.get("bet_type", "")).strip(), str(p.get("pick", "")).strip(), int(round(odds_val)),
            p.get("implied_prob", ""), model_prob_str, p.get("expected_value", ""),
            qk_units, "PENDING", 0.0, str(p.get("reasoning", "")).strip(), "NEW", p.get("high_agreement", "No"),
            str(p.get("start_time", "")).strip(),
            cache_data.get("away_bp", "N/A"),           
            cache_data.get("home_bp", "N/A"),           
            cache_data.get("math", "N/A"),              
            p.get("ai_contextual_shift", "")            
        ], value_input_option="USER_ENTERED")
        appended += 1
        existing_games.append(game)
            
    print(f"Execution complete! Added {appended} new pick(s).")

if __name__ == "__main__":
    main()
