import os
import json
import re
import time
import math
import requests
import gspread
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from google import genai
from google.genai import errors
from google.oauth2.service_account import Credentials

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

ALLOWED_SPORTSBOOKS = ["FanDuel", "DraftKings", "BetMGM", "Caesars"]

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

# --- POISSON DISTRIBUTION MATH ENGINE ---
def poisson_probability(lam, k):
    if lam <= 0: return 0.0
    return (math.exp(-lam) * (lam ** int(k))) / math.factorial(int(k))

def calculate_runline_prob(lam_fav, lam_dog):
    prob_cover = 0.0
    for f in range(2, 21):
        for d in range(0, f - 1):
            prob_cover += poisson_probability(lam_fav, f) * poisson_probability(lam_dog, d)
    return max(0.01, min(0.99, prob_cover))

# --- 1. GOOGLE SHEETS SETUP ---
def get_sheets():
    print("Connecting to Google Sheets ('Daily' & 'MLB' Tabs)...")
    service_account_str = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    if not service_account_str: raise ValueError("GCP_SERVICE_ACCOUNT_JSON missing!")
    
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    
    creds_dict = json.loads(service_account_str)
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(credentials)
    
    spreadsheet = client.open("MLB AI Betting Tracker")
    
    try: daily_sheet = spreadsheet.worksheet("Daily")
    except Exception: daily_sheet = spreadsheet.add_worksheet(title="Daily", rows=500, cols=25)
    
    try: mlb_sheet = spreadsheet.worksheet("MLB")
    except Exception: mlb_sheet = spreadsheet.add_worksheet(title="MLB", rows=500, cols=25)
        
    return spreadsheet, daily_sheet, mlb_sheet

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
    except Exception: pass

def ensure_evolution_sheet(spreadsheet):
    try:
        try: evo_sheet = spreadsheet.worksheet("Evolution & Learnings")
        except Exception: evo_sheet = spreadsheet.add_worksheet(title="Evolution & Learnings", rows=200, cols=10)
        if not evo_sheet.get_all_values():
            evo_sheet.insert_row(["Timestamp", "Sport", "Total Bets Evaluated", "Win Rate (%)", "Net Profit ($)", "Reasoning Factor Weights", "Active Strategy Adjustment", "Validation & Re-Synthesis Notes"], index=1)
        return evo_sheet
    except Exception: return None

# --- 2. ADVANCED METRICS & API SCRAPERS ---
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
                
                metrics_map[canonical] = {"ops": 0.720, "iso": 0.150, "whip": 1.30, "runs_per_game": 4.5}
                stats_url = f"https://statsapi.mlb.com/api/v1/teams/{t_id}/stats?stats=season&group=hitting,pitching"
                stats_resp = requests.get(stats_url, headers=headers, timeout=5)
                
                if stats_resp.status_code == 200:
                    for stat_group in stats_resp.json().get("stats", []):
                        group_name = stat_group.get("group", {}).get("displayName")
                        splits = stat_group.get("splits", [])
                        if not splits: continue
                        stat_data = splits[0].get("stat", {})
                        if group_name == "hitting":
                            avg = float(stat_data.get("avg", ".240"))
                            slg = float(stat_data.get("slg", ".400"))
                            metrics_map[canonical]["ops"] = float(stat_data.get("ops", ".720"))
                            metrics_map[canonical]["iso"] = round(slg - avg, 3)
                            metrics_map[canonical]["runs_per_game"] = float(stat_data.get("runsScoredPerGame", "4.5"))
                        elif group_name == "pitching":
                            metrics_map[canonical]["whip"] = float(stat_data.get("whip", "1.30"))
    except Exception: pass
    return metrics_map

def fetch_pitcher_season_stats(pitcher_name):
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        search_url = f"https://statsapi.mlb.com/api/v1/people/search?names={pitcher_name}&sportIds=1"
        search_resp = requests.get(search_url, headers=headers, timeout=5)
        if search_resp.status_code == 200:
            people = search_resp.json().get("people", [])
            if people:
                pid = people[0].get("id")
                stat_url = f"https://statsapi.mlb.com/api/v1/people/{pid}/stats?stats=season&group=pitching"
                stat_resp = requests.get(stat_url, headers=headers, timeout=5)
                if stat_resp.status_code == 200:
                    splits = stat_resp.json().get("stats", [])[0].get("splits", [])
                    if splits:
                        p_stats = splits[0].get("stat", {})
                        return {
                            "whip": float(p_stats.get("whip", 1.30)),
                            "era": float(p_stats.get("era", 4.00))
                        }
    except Exception: pass
    return {"whip": 1.30, "era": 4.00}

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

def get_mlb_teams_map():
    url = "https://statsapi.mlb.com/api/v1/teams?sportId=1"
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    teams = {}
    if resp.status_code == 200:
        for t in resp.json().get("teams", []):
            teams[t["id"]] = match_canonical_team(t["name"])
    return teams

# --- 3. BULLPEN SCRAPER ---
def fetch_situational_fatigue_and_bullpen(days_back_bp=2, days_back_schedule=7):
    teams_map = get_mlb_teams_map()
    today = datetime.now(ZoneInfo("America/New_York")).date()
    headers = {"User-Agent": "Mozilla/5.0"}
    team_stats = {name: {"appearances": 0, "total_pitches": 0, "bp_dates": set(), "schedule_games_7d": 0} for name in teams_map.values()}
    
    for d in range(1, days_back_schedule + 1):
        target_date = (today - timedelta(days=d)).strftime("%Y-%m-%d")
        schedule_resp = requests.get(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={target_date}", headers=headers, timeout=10)
        if schedule_resp.status_code != 200: continue
        dates = schedule_resp.json().get("dates", [])
        if not dates: continue

        for game in dates[0].get("games", []):
            if game.get("status", {}).get("abstractGameState") != "Final": continue
            game_pk = game.get("gamePk")
            
            for side in ["away", "home"]:
                team_id = game.get("teams", {})[side].get("team", {}).get("id")
                canonical = teams_map.get(team_id)
                if canonical:
                    team_stats[canonical]["schedule_games_7d"] += 1

            if d <= days_back_bp:
                box_resp = requests.get(f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore", headers=headers, timeout=10)
                if box_resp.status_code != 200: continue
                box_data = box_resp.json()

                for side in ["away", "home"]:
                    team_box = box_data.get("teams", {})[side]
                    team_id = team_box.get("team", {}).get("id")
                    canonical = teams_map.get(team_id)
                    if not canonical: continue

                    pitchers = team_box.get("pitchers", [])
                    players = team_box.get("players", {})

                    if len(pitchers) > 1:
                        relief_pitcher_ids = pitchers[1:]
                        game_relief_pitches = 0
                        for pid in relief_pitcher_ids:
                            p_info = players.get(f"ID{pid}", {})
                            p_stats = p_info.get("stats", {}).get("pitching", {})
                            pitches = int(p_stats.get("pitches", p_stats.get("numberOfPitches", 0)))
                            game_relief_pitches += pitches

                        if game_relief_pitches > 0:
                            team_stats[canonical]["total_pitches"] += game_relief_pitches
                            team_stats[canonical]["bp_dates"].add(target_date)
                            team_stats[canonical]["appearances"] += len(relief_pitcher_ids)

    objective_ratings = {}
    for team, stats in team_stats.items():
        total_p = stats["total_pitches"]
        apps = stats["appearances"]
        sched_games = stats["schedule_games_7d"]
        days_played = max(1.0, float(len(stats["bp_dates"])))
        load = round(float(total_p) / days_played, 1) if total_p > 0 else 0.0
        
        if load >= 45.0 or len(stats["bp_dates"]) >= 2: status = "TAXED"
        elif load >= 20.0: status = "MODERATELY WORKED"
        else: status = "FRESH"

        status_string = f"Status: {status} | Load Index: {load} | Relief Apps: {apps} | Total Pitches (2 Days): {total_p} | Games Played (Last 7 Days): {sched_games}"
        objective_ratings[team] = {
            "status_string": status_string,
            "load": load,
            "closer_b2b": len(stats["bp_dates"]) >= 2
        }
    return objective_ratings

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

# --- 4. THE 6-METRIC BASELINE MATH ENGINE ---
def calculate_strict_baseline(away, home, a_pitcher_name, h_pitcher_name, fatigue_data, advanced_metrics, memory):
    home_prob = 0.50 
    math_log = []
    weights = memory.get("reasoning_factor_weights", {})
    
    bp_weight = weights.get("bullpen_depth_and_fatigue", {}).get("weight", 1.0)
    ops_weight = weights.get("platoon_and_lineup_splits", {}).get("weight", 1.0)
    whip_weight = weights.get("starting_pitcher_expected_metrics", {}).get("weight", 1.0)
    
    a_sp_stats = fetch_pitcher_season_stats(a_pitcher_name)
    h_sp_stats = fetch_pitcher_season_stats(h_pitcher_name)
    sp_shift_raw = ((a_sp_stats["whip"] - h_sp_stats["whip"]) / 0.10) * 0.02 * whip_weight
    sp_shift = max(-0.10, min(0.10, sp_shift_raw))
    home_prob += sp_shift
    math_log.append(f"SP WHIP Shift ({a_sp_stats['whip']} vs {h_sp_stats['whip']}): {round(sp_shift*100, 2)}%")

    a_ops = advanced_metrics.get(away, {}).get("ops", 0.720)
    h_ops = advanced_metrics.get(home, {}).get("ops", 0.720)
    ops_shift_raw = ((h_ops - a_ops) / 0.050) * 0.015 * ops_weight
    ops_shift = max(-0.08, min(0.08, ops_shift_raw))
    home_prob += ops_shift
    math_log.append(f"OPS Shift ({a_ops} vs {h_ops}): {round(ops_shift*100, 2)}%")

    a_iso = advanced_metrics.get(away, {}).get("iso", 0.150)
    h_iso = advanced_metrics.get(home, {}).get("iso", 0.150)
    iso_shift_raw = ((h_iso - a_iso) / 0.020) * 0.01
    iso_shift = max(-0.05, min(0.05, iso_shift_raw))
    home_prob += iso_shift
    math_log.append(f"Contact Quality ISO Shift: {round(iso_shift*100, 2)}%")

    a_load = fatigue_data.get(away, {}).get("load", 15.0)
    h_load = fatigue_data.get(home, {}).get("load", 15.0)
    bp_shift_raw = ((a_load - h_load) / 50.0) * 0.015 * bp_weight
    bp_shift = max(-0.06, min(0.06, bp_shift_raw))
    home_prob += bp_shift
    math_log.append(f"Bullpen Load Shift: {round(bp_shift*100, 2)}%")

    a_games_7d = fatigue_data.get(away, {}).get("schedule_games", 5)
    h_games_7d = fatigue_data.get(home, {}).get("schedule_games", 5)
    sched_shift_raw = ((a_games_7d - h_games_7d) * 0.005) 
    sched_shift = max(-0.04, min(0.04, sched_shift_raw))
    home_prob += sched_shift
    math_log.append(f"Situational Schedule Shift (Games: {a_games_7d}v{h_games_7d}): {round(sched_shift*100, 2)}%")

    home_prob += 0.015
    math_log.append("HFA: +1.50%")
    
    # HARD CAPPED to enforce realistic market baseline probabilities
    home_prob = max(0.40, min(0.60, home_prob))
    away_prob = 1.0 - home_prob
    
    a_gpg = advanced_metrics.get(away, {}).get("runs_per_game", 4.5)
    h_gpg = advanced_metrics.get(home, {}).get("runs_per_game", 4.5)
    projected_total = round(a_gpg + h_gpg, 1)
    
    return home_prob, away_prob, projected_total, " | ".join(math_log)

# --- 5. AUTO-GRADING ---
def auto_grade_pending_bets(sheet, odds_key):
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1: return 0
        headers = [h.strip() for h in rows[0]]
        status_idx = headers.index("Status")
        game_idx = headers.index("Game")
        bet_type_idx = headers.index("Bet Type / Sportsbook")
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
            bet_type = str(r[bet_type_idx]).strip().lower()
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
                    total_score = home_score + away_score
                    
                    status = "PENDING"
                    if "moneyline" in bet_type:
                        winner = home_team if home_score > away_score else away_team
                        status = "WIN" if match_canonical_team(pick_str).lower() == match_canonical_team(winner).lower() else "LOSS"
                    elif "spread" in bet_type or "run line" in bet_type:
                        favored_team = pick_str.rsplit(' ', 1)[0]
                        try: spread_val = float(pick_str.split(' ')[-1])
                        except: spread_val = 0.0
                        h_covered = (home_score + spread_val) > away_score if match_canonical_team(favored_team).lower() == match_canonical_team(home_team).lower() else (away_score + spread_val) > home_score
                        status = "WIN" if h_covered else "LOSS"
                    elif "total" in bet_type or "over" in bet_type or "under" in bet_type:
                        try: line_val = float(pick_str.split(' ')[-1])
                        except: line_val = 0.0
                        is_over = "over" in pick_str.lower()
                        if is_over: status = "WIN" if total_score > line_val else "LOSS" if total_score < line_val else "PENDING"
                        else: status = "WIN" if total_score < line_val else "LOSS" if total_score > line_val else "PENDING"

                    if status != "PENDING":
                        profit = ((odds / 100.0) * 100.0 * units) if (status == "WIN" and odds > 0) else ((100.0 / abs(odds)) * 100.0 * units) if status == "WIN" else (-100.0 * units)
                        updates.append({"range": f"K{row_idx}:L{row_idx}", "values": [[status, round(profit, 2)]]})
                    break
        if updates: sheet.batch_update(updates)
    except Exception: pass

def get_today_existing_picks_detailed(sheet, today_date_str):
    rows = sheet.get_all_values()
    if len(rows) <= 1: return []
    existing = []
    for idx, r in enumerate(rows[1:], start=2):
        if len(r) > 10 and str(r[0]).strip() == today_date_str and str(r[10]).strip().upper() == "PENDING":
            existing.append({
                "row_index": idx,
                "game": str(r[2]).strip(),
                "bet_type": str(r[3]).strip(),
                "pick": str(r[4]).strip(),
                "odds": float(r[5]) if r[5] else -110.0
            })
    return existing

# --- 6. MATCHUP FORMATTING ---
def fetch_mlb_odds(odds_key):
    resp = requests.get(f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/?apiKey={odds_key}&regions=us&markets=h2h,spreads,totals&oddsFormat=american")
    return resp.json() if resp.status_code == 200 else []

def format_matchups(odds_data, probable_pitchers, objective_fatigue_ratings, advanced_metrics, memory, today_date_str):
    valid = []
    matchup_cache = {}  
    current_utc = datetime.now(ZoneInfo("America/New_York"))
    
    for game in odds_data:
        raw_home = game.get("home_team", "")
        raw_away = game.get("away_team", "")
        home = match_canonical_team(raw_home)
        away = match_canonical_team(raw_away)
        
        commence_time_str = game.get("commence_time")
        game_time_et = "Unknown Time"
        game_date_et = ""
        
        if commence_time_str:
            try:
                dt_utc = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
                if dt_utc < current_utc: continue
                dt_et = dt_utc.astimezone(ZoneInfo("America/New_York"))
                game_date_et = dt_et.strftime("%Y-%m-%d")
                game_time_et = dt_et.strftime("%Y-%m-%d %I:%M %p EDT")
            except Exception: pass

        # Strict Date Filter
        if game_date_et != today_date_str:
            continue

        h_pitcher = probable_pitchers.get(home, "TBD")
        a_pitcher = probable_pitchers.get(away, "TBD")
        
        home_prob, away_prob, projected_total, math_log = calculate_strict_baseline(away, home, a_pitcher, h_pitcher, objective_fatigue_ratings, advanced_metrics, memory)
        
        lam_home = projected_total * home_prob
        lam_away = projected_total * away_prob
        home_rl_cover = calculate_runline_prob(lam_home, lam_away)
        away_rl_cover = calculate_runline_prob(lam_away, lam_home)
        
        default_bp = {"status_string": "Status: FRESH | Load Index: 0.0 | Relief Apps: 0 | Total Pitches (2 Days): 0 | Games Played (Last 7 Days): 0"}
        away_bp_data = objective_fatigue_ratings.get(away, objective_fatigue_ratings.get(raw_away, default_bp))
        home_bp_data = objective_fatigue_ratings.get(home, objective_fatigue_ratings.get(raw_home, default_bp))
        
        away_bp_str = away_bp_data.get('status_string', default_bp['status_string'])
        home_bp_str = home_bp_data.get('status_string', default_bp['status_string'])
        
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
            "projected_total_runs": projected_total,
            "home_runline_cover_prob_baseline": f"{round(home_rl_cover * 100, 1)}%",
            "away_runline_cover_prob_baseline": f"{round(away_rl_cover * 100, 1)}%",
            "calculation_log": math_log
        }
        valid.append(game_copy)
    return valid, matchup_cache

# --- 7. AI SYNTHESIS ---
def parse_json_from_response(response):
    raw_text = getattr(response, "text", "")
    if hasattr(response, "candidates") and response.candidates:
        raw_text = "".join([p.text for p in response.candidates[0].content.parts if hasattr(p, "text") and p.text])
        
    marker = "`" * 3
    json_match = re.search(rf'{marker}(?:json)?\s*(.*?)\s*{marker}', raw_text.strip(), re.DOTALL)
    if json_match:
        try: return json.loads(json_match.group(1))
        except Exception: pass
        
    clean_text = raw_text.replace(f"{marker}json", "").replace(marker, "").strip()
    try: 
        return json.loads(clean_text)
    except Exception:
        return {}

def generate_daily_and_mlb_picks(formatted_games, open_picks, memory):
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    prompt = f"""
    You are an elite Bounded Multi-Factor Sports Betting Analyst. Python has implemented Poisson distributions to ground your projections. Do NOT output any probability above 65% for any market.

    === TODAY'S MATCHUPS, MARKET ODDS & BASELINES ===
    {json.dumps(formatted_games, indent=2)}

    === ACTIVE PENDING PICKS TO RE-EVALUATE ===
    {json.dumps(open_picks, indent=2)}

    STRICT RULES:
    1. APPROVED SPORTSBOOKS ONLY: Pick ONLY from: {ALLOWED_SPORTSBOOKS}.
    2. THE LEASH (±5.0% MAX): Adjust probabilities by a maximum of ± 5.0% based on holistic contextual intuition.
    3. NO ARTIFICIAL EV MANIPULATION: Calculate the true Expected Value (EV) strictly as: [(Model Prob * Decimal Odds) - 1]. DO NOT reverse-engineer probabilities to fake an 11% EV.
    4. THE 11.0% THRESHOLD & NO FORCED PICKS: You do NOT have to generate a pick for every game. Evaluate all markets (Moneyline, Run Line, Totals). ONLY select a pick if its true, natural EV is >= 11.0%. Skip games with no value.
    5. MAX-EV SIDE SELECTION: If both the Moneyline and Run Line for the same team clear the 11.0% EV threshold, you MUST ONLY output the ONE market with the HIGHEST EV. Do not pick both.
    6. TAB FORMATTING: Output your final list of >= 11.0% EV picks into BOTH the `daily_tab_picks` and `mlb_tab_picks` arrays.
    7. MANDATORY VALIDATION: For each item in 'ACTIVE PENDING PICKS TO RE-EVALUATE', check if current odds/baselines still sustain an EV >= 11.0%. Output action "VALIDATED" or "REJECTED".

    OUTPUT SCHEMA (STRICT JSON):
    {{
      "validations": [
        {{
          "row_index": <int matching row_index in open_picks>,
          "action": "VALIDATED" or "REJECTED",
          "updated_odds": <int or float>,
          "updated_model_prob": "58.0%",
          "updated_expected_value": "+11.2%",
          "reason": "<tight summary>"
        }}
      ],
      "daily_tab_picks": [
        {{
          "date": "YYYY-MM-DD",
          "start_time": "YYYY-MM-DD HH:MM PM EDT",
          "game": "Away Team @ Home Team",
          "bet_type": "Moneyline (FanDuel)" or "Run Line (DraftKings)" or "Total Over (BetMGM)",
          "pick": "Team Name" or "Team Name -1.5" or "Over 8.5",
          "odds": 140,
          "implied_prob": "41.6%",
          "model_prob": "55.0%",
          "expected_value": "+13.2%",
          "high_agreement": "Consensus",
          "reasoning": "High-EV justification",
          "ai_contextual_shift": "Shifted +X%"
        }}
      ],
      "mlb_tab_picks": [
        {{
          "date": "YYYY-MM-DD",
          "start_time": "YYYY-MM-DD HH:MM PM EDT",
          "game": "Away Team @ Home Team",
          "bet_type": "Moneyline (FanDuel)",
          "pick": "Team Name",
          "odds": 140,
          "implied_prob": "41.6%",
          "model_prob": "55.0%",
          "expected_value": "+13.2%",
          "high_agreement": "Consensus",
          "reasoning": "High-EV justification",
          "ai_contextual_shift": "Shifted +X%"
        }}
      ]
    }}
    """
    for model_name in ["gemini-3.1-pro-preview", "gemini-3.7-flash"]:
        for attempt in range(2):
            try:
                print(f"Attempting synthesis with model: {model_name} (Attempt {attempt+1})...")
                response = client.models.generate_content(model=model_name, contents=prompt)
                print(f"Successfully synthesized matchups using {model_name}...")
                return parse_json_from_response(response)
            except Exception: time.sleep(5)
    return {"validations": [], "daily_tab_picks": [], "mlb_tab_picks": []}

# --- 8. MAIN EXECUTION ---
def main():
    spreadsheet, daily_sheet, mlb_sheet = get_sheets()
    ensure_headers(daily_sheet)
    ensure_headers(mlb_sheet)
    
    odds_key = os.environ.get("ODDS_API_KEY")
    auto_grade_pending_bets(daily_sheet, odds_key)
    auto_grade_pending_bets(mlb_sheet, odds_key)
    
    memory = load_memory()
    today_date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    current_time_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S EDT")
    
    probable_pitchers = fetch_today_probable_pitchers(today_date_str)
    advanced_metrics = fetch_team_advanced_metrics()
    fatigue_data = fetch_situational_fatigue_and_bullpen(days_back_bp=2, days_back_schedule=7)
    
    odds = fetch_mlb_odds(odds_key)
    if not odds: return

    formatted_games, matchup_cache = format_matchups(odds, probable_pitchers, fatigue_data, advanced_metrics, memory, today_date_str)
    if not formatted_games:
        print("No eligible games found for today.")
        return

    open_picks_detailed = get_today_existing_picks_detailed(daily_sheet, today_date_str)
    ai_response = generate_daily_and_mlb_picks(formatted_games, open_picks_detailed, memory)
    
    # Process Validations
    validations = ai_response.get("validations", [])
    if validations:
        print(f"Processing {len(validations)} pick validation(s)...")
        for val in validations:
            row_idx = val.get("row_index")
            action = str(val.get("action", "")).strip().upper()
            reason = str(val.get("reason", "")).strip()
            for sheet_obj in [daily_sheet, mlb_sheet]:
                try:
                    if row_idx and action in ["VALIDATED", "REJECTED"]:
                        sheet_obj.update_cell(row_idx, 14, action)
                        if action == "VALIDATED":
                            if val.get("updated_odds"): sheet_obj.update_cell(row_idx, 6, int(round(float(val.get("updated_odds")))))
                            if val.get("updated_model_prob"): sheet_obj.update_cell(row_idx, 8, val.get("updated_model_prob"))
                            if val.get("updated_expected_value"): sheet_obj.update_cell(row_idx, 9, val.get("updated_expected_value"))
                            if reason: sheet_obj.update_cell(row_idx, 13, reason)
                            sheet_obj.update_cell(row_idx, 2, current_time_str)
                        elif action == "REJECTED":
                            sheet_obj.update_cell(row_idx, 11, "REJECTED")
                            sheet_obj.update_cell(row_idx, 12, 0.0)
                            if reason: sheet_obj.update_cell(row_idx, 13, reason)
                            sheet_obj.update_cell(row_idx, 2, current_time_str)
                except Exception: pass

    def write_picks_to_sheet(picks_list, target_sheet):
        appended = 0
        existing_rows = target_sheet.get_all_values()
        existing_signatures = [f"{r[2]} | {r[3]}" for r in existing_rows[1:]] if len(existing_rows) > 1 else []
        
        for p in picks_list:
            game = str(p.get("game", "")).strip()
            bet_type_label = str(p.get("bet_type", "")).strip()
            
            book_matched = any(sb.lower() in bet_type_label.lower() for sb in ALLOWED_SPORTSBOOKS)
            if not book_matched: continue

            sig = f"{game} | {bet_type_label}"
            if sig in existing_signatures: continue

            pick_date = str(p.get("date", today_date_str)).strip()
            model_prob_str = str(p.get("model_prob", "50.0%"))
            try: odds_val = float(p.get("odds", -110))
            except: odds_val = -110.0

            qk_units = compute_quarter_kelly_units(odds_val, model_prob_str)
            
            cache_data = {}
            gemini_game_norm = normalize_text(game)
            for cached_key, data in matchup_cache.items():
                cached_teams = [normalize_text(t) for t in cached_key.split(" @ ")]
                if len(cached_teams) == 2 and cached_teams[0] in gemini_game_norm and cached_teams[1] in gemini_game_norm:
                    cache_data = data
                    break

            target_sheet.append_row([
                pick_date, current_time_str, game, bet_type_label, str(p.get("pick", "")).strip(), int(round(odds_val)),
                p.get("implied_prob", ""), model_prob_str, p.get("expected_value", ""),
                qk_units, "PENDING", 0.0, str(p.get("reasoning", "")).strip(), "NEW", p.get("high_agreement", "No"),
                str(p.get("start_time", "")).strip(),
                cache_data.get("away_bp", "Status: FRESH | Load Index: 0.0 | Relief Apps: 0 | Total Pitches (2 Days): 0 | Games Played (Last 7 Days): 0"),           
                cache_data.get("home_bp", "Status: FRESH | Load Index: 0.0 | Relief Apps: 0 | Total Pitches (2 Days): 0 | Games Played (Last 7 Days): 0"),           
                cache_data.get("math", "N/A"),              
                p.get("ai_contextual_shift", "")            
            ], value_input_option="USER_ENTERED")
            appended += 1
            existing_signatures.append(sig)
        return appended

    daily_picks = ai_response.get("daily_tab_picks", [])
    daily_count = write_picks_to_sheet(daily_picks, daily_sheet)
    
    mlb_picks = ai_response.get("mlb_tab_picks", [])
    mlb_count = write_picks_to_sheet(mlb_picks, mlb_sheet)
            
    print(f"Execution complete! Added {daily_count} natural picks to 'Daily' and {mlb_count} high-EV picks to 'MLB'.")

if __name__ == "__main__":
    main()
