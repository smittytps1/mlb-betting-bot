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
        
        # Sizing anchor: ~10% Kelly edge produces ~1.0 unit
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
            "Reasoning", "Validation", "High Agreement & Source Breakdown", "Game Start Time"
        ]
        
        if not existing or not existing[0] or existing[0][0] != "Date": 
            sheet.insert_row(headers, index=1)
        else:
            current_headers = existing[0]
            if "Game Start Time" not in current_headers:
                sheet.update_cell(1, 16, "Game Start Time")
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

def update_evolution_log(spreadsheet, sport_label, memory, summary, time_str):
    try:
        evo_sheet = ensure_evolution_sheet(spreadsheet)
        if not evo_sheet: return
        
        factors = memory.get("reasoning_factor_weights", {})
        if factors:
            weights_str = " | ".join([f"{k}: {v.get('weight', 1.0)}x" for k, v in factors.items()])
        else:
            weights_str = "Standard (1.0x)"

        evo_sheet.append_row([
            time_str, 
            sport_label, 
            memory.get("total_bets", 0), 
            memory.get("win_rate", "0%"), 
            memory.get("net_profit_dollars", 0.0), 
            weights_str, 
            memory.get("learnings_and_adjustments", "Maintain balanced quantitative multi-factor evaluation."), 
            summary
        ])
    except Exception as e:
        print(f"Notice while logging to Evolution tab: {e}")

# --- 2. MULTI-SOURCE PROBABLES (ESPN + MLB STATS API) ---
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
    except Exception as e: print(f"ESPN Probables notice: {e}")

    try:
        mlb_url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={target_date_str}&hydrate=probablePitcher(note)"
        mlb_resp = requests.get(mlb_url, headers=headers, timeout=10)
        
        if mlb_resp.status_code == 200:
            dates = mlb_resp.json().get("dates", [])
            if dates:
                for game in dates[0].get("games", []):
                    teams = game.get("teams", {})
                    for side in ["away", "home"]:
                        team_data = teams.get(side, {})
                        raw_team = team_data.get("team", {}).get("name", "")
                        canonical = match_canonical_team(raw_team)
                        
                        if canonical and (canonical not in pitcher_map or "TBD" in pitcher_map[canonical]):
                            pitcher_info = team_data.get("probablePitcher", {})
                            if pitcher_info:
                                p_name = pitcher_info.get("fullName", "TBD")
                                pitcher_map[canonical] = p_name
    except Exception as e:
        print(f"MLB API Probables notice: {e}")

    return pitcher_map

# --- 3. HIGH-LEVERAGE BULLPEN ENGINE ---
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
    except Exception:
        pass
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

                # FIX: Initialize sets to track the actual dates pitchers threw
                if canonical not in team_stats:
                    team_stats[canonical] = {"raw_pitches": 0, "weighted_load": 0.0, "closer_dates": set(), "setup_dates": set()}

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
                            team_stats[canonical]["closer_dates"].add(target_date)
                        elif p_name in setup_names:
                            weight = 2.0
                            team_stats[canonical]["setup_dates"].add(target_date)
                        else:
                            weight = 1.0

                        team_stats[canonical]["raw_pitches"] += pitches
                        team_stats[canonical]["weighted_load"] += (pitches * weight)

    objective_ratings = {}
    for team, stats in team_stats.items():
        load = stats["weighted_load"]
        # FIX: Only flag as B2B True if they pitched on 2 distinct days
        c_b2b = len(stats["closer_dates"]) >= 2
        s_b2b = len(stats["setup_dates"]) >= 2

        if c_b2b or load >= 100: status = f"TAXED / FATIGUED (Closer B2B Burn: {c_b2b})"
        elif s_b2b or load >= 60: status = "MODERATELY WORKED (Setup Men Used)"
        else: status = "FRESH / RESTED (Shutdown Arms Available)"

        objective_ratings[team] = f"Status: {status} | Weighted Backend Load: {round(load, 1)}"

    return objective_ratings

# --- 4. ACCURATE AUTO-GRADING WITH DATE & TIME MATCHING ---
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
        start_time_idx = headers.index("Game Start Time") if "Game Start Time" in headers else -1
        
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
            logged_start_time = str(r[start_time_idx]).strip() if start_time_idx != -1 and len(r) > start_time_idx else ""
            
            try: odds = float(r[odds_idx])
            except: odds = -110.0
            
            try: units = float(r[units_idx]) if r[units_idx] else 1.0
            except: units = 1.0

            for match in scores_data:
                if not match.get("completed"): continue
                
                commence_time_str = match.get("commence_time", "")
                match_time_et_str = ""
                match_date_ny_str = ""
                
                if commence_time_str:
                    try:
                        game_dt_utc = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
                        game_dt_ny = game_dt_utc.astimezone(ZoneInfo("America/New_York"))
                        match_date_ny_str = game_dt_ny.strftime("%Y-%m-%d")
                        match_time_et_str = game_dt_ny.strftime("%Y-%m-%d %I:%M %p EDT")
                    except Exception:
                        pass
                
                if logged_start_time:
                    if logged_start_time != match_time_et_str and not logged_start_time.startswith(match_date_ny_str): continue
                else:
                    if pick_date_str != match_date_ny_str: continue

                home_team = match.get("home_team", "")
                away_team = match.get("away_team", "")
                home_canonical = match_canonical_team(home_team)
                away_canonical = match_canonical_team(away_team)

                if (home_canonical in game_title or away_canonical in game_title or home_team in game_title or away_team in game_title):
                    scores = match.get("scores")
                    if not scores or len(scores) < 2: continue
                    home_score = next((int(s["score"]) for s in scores if s["name"] == home_team), 0)
                    away_score = next((int(s["score"]) for s in scores if s["name"] == away_team), 0)
                    total_score = home_score + away_score
                    status = None
                    profit = 0.0
                    pick_lower = pick_str.lower()
                    is_total = ("total" in bet_type or "over" in pick_lower or "under" in pick_lower or "o/u" in pick_lower)

                    if is_total:
                        num_match = re.search(r'(?:over|under|o/u|u|o)?\s*([0-9]+\.?[0-9]*)', pick_lower)
                        if num_match:
                            total_line = float(num_match.group(1))
                            is_over = bool(re.search(r'\b(over|o)\b', pick_lower)) or "over" in pick_lower
                            is_under = bool(re.search(r'\b(under|u)\b', pick_lower))
                            if total_score == total_line: status = "PUSH"
                            elif (is_over and total_score > total_line) or (is_under and total_score < total_line): status = "WIN"
                            else: status = "LOSS"
                    elif "spread" in bet_type or "run line" in bet_type or re.search(r'[-+]\d+\.?\d*', pick_str):
                        spread_match = re.search(r'([-+]\s*\d+\.?\d*)', pick_str)
                        spread_val = float(spread_match.group(1).replace(" ", "")) if spread_match else 0.0
                        is_home_pick = (home_canonical.lower() in pick_lower or home_team.lower() in pick_lower)
                        pick_score = home_score if is_home_pick else away_score
                        opp_score = away_score if is_home_pick else home_score
                        diff = (pick_score + spread_val) - opp_score
                        if diff == 0: status = "PUSH"
                        elif diff > 0: status = "WIN"
                        else: status = "LOSS"
                    else:
                        winner = home_team if home_score > away_score else away_team
                        is_win = (match_canonical_team(pick_str).lower() == match_canonical_team(winner).lower() or pick_lower in winner.lower() or winner.lower() in pick_lower)
                        status = "WIN" if is_win else "LOSS"

                    if status == "WIN":
                        if odds < 0: profit = (100.0 / abs(odds)) * 100.0 * units
                        else: profit = (odds / 100.0) * 100.0 * units
                    elif status == "LOSS": profit = -100.0 * units
                    elif status == "PUSH": profit = 0.0

                    updates.append({"range": f"K{row_idx}:L{row_idx}", "values": [[status, round(profit, 2)]]})
                    break

        if updates:
            sheet.batch_update(updates)
            return len(updates)
    except Exception as e:
        print(f"Auto-grade notice: {e}")
    return 0

# --- 5. ROBUST UNIVERSAL MULTI-TIMEFRAME SCOREBOARD ---
def update_scoreboard(spreadsheet):
    try:
        try: sb = spreadsheet.worksheet("Scoreboard")
        except: sb = spreadsheet.add_worksheet(title="Scoreboard", rows=20, cols=10)
        
        scoreboard_data = [
            ["Bot / Sport & Timeframe", "Correct Picks (Wins)", "Incorrect Picks (Losses)", "Pending Bets", "Win Rate (%)", "Total Money Won / Lost ($)"],
            ["MLB Bot (All-Time)", '=COUNTIF(MLB!K:K, "WIN")', '=COUNTIF(MLB!K:K, "LOSS")', '=COUNTIF(MLB!K:K, "PENDING")', '=IFERROR(B2/(B2+C2), 0)', '=SUM(MLB!L:L)'],
            ["MLB Bot (7-Day / 1-Week)", '=SUMPRODUCT((MLB!K2:K="WIN")*(IFERROR(DATEVALUE(MLB!A2:A),IFERROR(VALUE(MLB!A2:A),0))>=TODAY()-7)*(MLB!A2:A<>""))', '=SUMPRODUCT((MLB!K2:K="LOSS")*(IFERROR(DATEVALUE(MLB!A2:A),IFERROR(VALUE(MLB!A2:A),0))>=TODAY()-7)*(MLB!A2:A<>""))', '=SUMPRODUCT((MLB!K2:K="PENDING")*(IFERROR(DATEVALUE(MLB!A2:A),IFERROR(VALUE(MLB!A2:A),0))>=TODAY()-7)*(MLB!A2:A<>""))', '=IFERROR(B3/(B3+C3), 0)', '=SUMPRODUCT((IFERROR(DATEVALUE(MLB!A2:A),IFERROR(VALUE(MLB!A2:A),0))>=TODAY()-7)*(MLB!A2:A<>"")*(IFERROR(VALUE(MLB!L2:L),0)))'],
            ["MLB Bot (3-Day)", '=SUMPRODUCT((MLB!K2:K="WIN")*(IFERROR(DATEVALUE(MLB!A2:A),IFERROR(VALUE(MLB!A2:A),0))>=TODAY()-3)*(MLB!A2:A<>""))', '=SUMPRODUCT((MLB!K2:K="LOSS")*(IFERROR(DATEVALUE(MLB!A2:A),IFERROR(VALUE(MLB!A2:A),0))>=TODAY()-3)*(MLB!A2:A<>""))', '=SUMPRODUCT((MLB!K2:K="PENDING")*(IFERROR(DATEVALUE(MLB!A2:A),IFERROR(VALUE(MLB!A2:A),0))>=TODAY()-3)*(MLB!A2:A<>""))', '=IFERROR(B4/(B4+C4), 0)', '=SUMPRODUCT((IFERROR(DATEVALUE(MLB!A2:A),IFERROR(VALUE(MLB!A2:A),0))>=TODAY()-3)*(MLB!A2:A<>"")*(IFERROR(VALUE(MLB!L2:L),0)))'],
            ["MLB Bot (1-Day / Today)", '=SUMPRODUCT((MLB!K2:K="WIN")*(IFERROR(DATEVALUE(MLB!A2:A),IFERROR(VALUE(MLB!A2:A),0))>=TODAY()-1)*(MLB!A2:A<>""))', '=SUMPRODUCT((MLB!K2:K="LOSS")*(IFERROR(DATEVALUE(MLB!A2:A),IFERROR(VALUE(MLB!A2:A),0))>=TODAY()-1)*(MLB!A2:A<>""))', '=SUMPRODUCT((MLB!K2:K="PENDING")*(IFERROR(DATEVALUE(MLB!A2:A),IFERROR(VALUE(MLB!A2:A),0))>=TODAY()-1)*(MLB!A2:A<>""))', '=IFERROR(B5/(B5+C5), 0)', '=SUMPRODUCT((IFERROR(DATEVALUE(MLB!A2:A),IFERROR(VALUE(MLB!A2:A),0))>=TODAY()-1)*(MLB!A2:A<>"")*(IFERROR(VALUE(MLB!L2:L),0)))']
        ]
        sb.clear()
        sb.update(range_name="A1:F5", values=scoreboard_data, value_input_option="USER_ENTERED")
    except Exception as e:
        print(f"Scoreboard notice: {e}")

# --- 6. RECURSIVE MEMORY & FACTOR WEIGHTING ---
def load_memory():
    if os.path.exists("bot_memory.json"):
        try:
            with open("bot_memory.json", "r") as f: return json.load(f)
        except Exception: pass
    
    default_memory = {
        "total_bets": 0, "wins": 0, "losses": 0, "win_rate": "0%", "net_profit_dollars": 0.0,
        "learnings_and_adjustments": "Maintain balanced quantitative multi-factor evaluation.",
        "reasoning_factor_weights": {
            "starting_pitcher_expected_metrics": {"wins": 0, "losses": 0, "weight": 1.0, "instruction": "Evaluate expected metrics."},
            "platoon_and_lineup_splits": {"wins": 0, "losses": 0, "weight": 1.0, "instruction": "Evaluate wRC+ and splits."},
            "statcast_contact_quality": {"wins": 0, "losses": 0, "weight": 1.0, "instruction": "Evaluate xwOBA and Hard-Hit%."},
            "multi_source_consensus_and_divergence": {"wins": 0, "losses": 0, "weight": 1.0, "instruction": "Evaluate model divergence."},
            "bullpen_depth_and_fatigue": {"wins": 0, "losses": 0, "weight": 1.0, "instruction": "Respect season-weighted ratings."},
            "umpire_and_situational_fatigue": {"wins": 0, "losses": 0, "weight": 1.0, "instruction": "Evaluate schedule fatigue and game times."}
        }
    }
    with open("bot_memory.json", "w") as f:
        json.dump(default_memory, f, indent=2)
    return default_memory

def calculate_factor_weight(wins, losses):
    total = wins + losses
    if total < 3: return 1.0, "Baseline sample size."
    win_rate = wins / total
    if win_rate >= 0.65: return min(1.5, round(1.0 + (win_rate - 0.5) * 1.0, 2)), f"High win rate ({round(win_rate*100, 1)}%). Prioritize this factor."
    elif win_rate <= 0.40: return max(0.3, round(1.0 - (0.5 - win_rate) * 1.2, 2)), f"Cold streak ({round(win_rate*100, 1)}%). De-emphasize."
    else: return 1.0, f"Neutral performance ({round(win_rate*100, 1)}%)."

def update_memory_from_sheet(sheet, memory):
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1: return memory

        headers = [h.strip() for h in rows[0]]
        status_idx = headers.index("Status") if "Status" in headers else 10
        pl_idx = headers.index("P/L ($)") if "P/L ($)" in headers else 11
        reason_idx = headers.index("Reasoning") if "Reasoning" in headers else 12

        wins = sum(1 for r in rows[1:] if len(r) > status_idx and str(r[status_idx]).strip().upper() == "WIN")
        losses = sum(1 for r in rows[1:] if len(r) > status_idx and str(r[status_idx]).strip().upper() == "LOSS")
        total = wins + losses

        factors = memory.get("reasoning_factor_weights", {})
        for key in factors:
            factors[key]["wins"] = 0
            factors[key]["losses"] = 0

        keywords_map = {
            "starting_pitcher_expected_metrics": ["xfip", "siera", "xera", "fip", "csw", "whip"],
            "platoon_and_lineup_splits": ["wrc+", "ops", "platoon", "vs lhp", "vs rhp", "lineup"],
            "statcast_contact_quality": ["statcast", "xwoba", "barrel", "hard-hit", "xba", "xslg"],
            "bullpen_depth_and_fatigue": ["bullpen", "reliever", "leverage", "closer", "backend", "load"],
            "umpire_and_situational_fatigue": ["umpire", "strike zone", "getaway day", "travel", "time"]
        }

        for r in rows[1:]:
            if len(r) > max(status_idx, reason_idx):
                status = str(r[status_idx]).strip().upper()
                reasoning = str(r[reason_idx]).lower()
                if status in ["WIN", "LOSS"]:
                    for factor_key, kws in keywords_map.items():
                        if any(kw in reasoning for kw in kws):
                            if factor_key not in factors: factors[factor_key] = {"wins": 0, "losses": 0, "weight": 1.0, "instruction": ""}
                            if status == "WIN": factors[factor_key]["wins"] += 1
                            else: factors[factor_key]["losses"] += 1

        for factor_key, data in factors.items():
            w_val, inst = calculate_factor_weight(data["wins"], data["losses"])
            data["weight"] = w_val
            data["instruction"] = inst

        if total > 0:
            memory["total_bets"] = total
            memory["wins"] = wins
            memory["losses"] = losses
            memory["win_rate"] = f"{round((wins / total) * 100, 1)}%"
            memory["net_profit_dollars"] = round(sum(float(r[pl_idx] or 0.0) for r in rows[1:] if len(r) > pl_idx and r[pl_idx]), 2)
        
        with open("bot_memory.json", "w") as f: json.dump(memory, f, indent=2)
    except Exception as e:
        print(f"Memory update notice: {e}")
    return memory

# --- 7. MATCHUP FORMATTING & STRICT TEMPORAL GUARDRAIL ---
def fetch_mlb_odds(odds_key):
    resp = requests.get(f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/?apiKey={odds_key}&regions=us&markets=h2h,spreads,totals&oddsFormat=american")
    return resp.json() if resp.status_code == 200 else []

def get_today_existing_picks(sheet, today_date_str):
    rows = sheet.get_all_values()
    if len(rows) <= 1: return []
    return [{"row_index": i, "date": r[0], "game": r[2], "status": r[10]} for i, r in enumerate(rows[1:], start=2) if r[0] == today_date_str and r[10] == "PENDING"]

def extract_canonical_teams_from_game(game_str):
    parts = re.split(r'\b(?:at|vs|v|@)\b', str(game_str), flags=re.IGNORECASE)
    cleaned = [match_canonical_team(p) for p in parts if p.strip()]
    return tuple(sorted(cleaned))

def game_already_pending(raw_rows, pick_date, game):
    if len(raw_rows) <= 1: return False
    headers = [h.strip() for h in raw_rows[0]]
    try:
        date_col, game_col, status_col = headers.index("Date"), headers.index("Game"), headers.index("Status")
    except ValueError: return False

    norm_teams = extract_canonical_teams_from_game(game)
    for r in raw_rows[1:]:
        if len(r) > max(date_col, game_col, status_col):
            if str(r[date_col]).strip() == pick_date and extract_canonical_teams_from_game(r[game_col]) == norm_teams and str(r[status_col]).strip().upper() == "PENDING":
                return True
    return False

def check_for_hallucinated_pitchers(game_str, reasoning_str, probable_pitchers):
    try:
        parts = game_str.split("@")
        if len(parts) != 2: return True
        away_canonical, home_canonical = match_canonical_team(parts[0].strip()), match_canonical_team(parts[1].strip())
        
        for team, pitcher_info in probable_pitchers.items():
            if team == home_canonical or team == away_canonical: continue
            foreign_pitcher_name = pitcher_info.split("(")[0].strip()
            if foreign_pitcher_name and foreign_pitcher_name.upper() != "TBD" and len(foreign_pitcher_name) > 4:
                if foreign_pitcher_name in reasoning_str:
                    print(f"  [GUARDRAIL TRIGGERED] Cross-wire detected! {foreign_pitcher_name} does not pitch in {game_str}.")
                    return False 
    except Exception:
        pass
    return True

def format_matchups(odds_data, probable_pitchers, objective_fatigue_ratings):
    valid = []
    dropped_tbd = []
    dropped_live = []
    current_utc = datetime.now(ZoneInfo("UTC"))
    
    for game in odds_data:
        home, away = match_canonical_team(game.get("home_team", "")), match_canonical_team(game.get("away_team", ""))
        commence_time_str = game.get("commence_time")
        game_time_et = "Unknown Time"
        
        if commence_time_str:
            try:
                dt_utc = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
                if dt_utc < current_utc:
                    dropped_live.append(f"{away} @ {home}")
                    continue
                dt_et = dt_utc.astimezone(ZoneInfo("America/New_York"))
                game_time_et = dt_et.strftime("%Y-%m-%d %I:%M %p EDT")
            except Exception:
                pass

        h_pitcher = probable_pitchers.get(home, "TBD")
        a_pitcher = probable_pitchers.get(away, "TBD")
        
        if "TBD" in h_pitcher or "TBD" in a_pitcher: 
            dropped_tbd.append(f"{away} @ {home}")
            continue
            
        game_copy = dict(game)
        game_copy["matchup_context"] = {
            "start_time": game_time_et,
            "away": f"{away} | Starter: {a_pitcher} | Bullpen: {objective_fatigue_ratings.get(away, 'Fresh')}",
            "home": f"{home} | Starter: {h_pitcher} | Bullpen: {objective_fatigue_ratings.get(home, 'Fresh')}"
        }
        valid.append(game_copy)
        
    if dropped_tbd: print(f"  [Python Guardrail] Dropped {len(dropped_tbd)} game(s) due to TBD starters.")
    if dropped_live: print(f"  [Temporal Guardrail] Dropped {len(dropped_live)} live/in-play game(s).")
    return valid

# --- 8. GEMINI PRO REASONING & SYNTHESIS ---
def parse_json_from_response(response):
    raw_text = ""
    if hasattr(response, "text") and response.text: raw_text = response.text
    elif hasattr(response, "candidates") and response.candidates:
        parts = response.candidates[0].content.parts
        raw_text = "".join([p.text for p in parts if hasattr(p, "text") and p.text])

    raw_text = raw_text.strip()
    json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
    if json_match:
        try: return json.loads(json_match.group(0))
        except Exception: pass
        
    marker = "`" * 3
    clean_text = raw_text.replace(f"{marker}json", "").replace(marker, "").strip()
    return json.loads(clean_text)

def generate_picks_and_validations(odds_data, memory, open_picks, fatigue_ratings, probable_pitchers):
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    
    formatted_games = format_matchups(odds_data, probable_pitchers, fatigue_ratings)
    if not formatted_games: 
        print("WARNING: No valid pre-game matchups found.")
        return {"validations": [], "new_picks": []}

    prompt = f"""
    You are an elite quantitative MLB betting engine executing deep multi-variable synthesis.

    === RECURSIVE MEMORY & FACTOR WEIGHTS ===
    {json.dumps(memory.get("reasoning_factor_weights", {}), indent=2)}

    === TODAY'S MATCHUPS & SEASON-WEIGHTED BULLPEN STATUS (WITH START TIMES) ===
    {json.dumps(formatted_games, indent=2)}

    === ACTIVE PENDING PICKS ===
    {json.dumps(open_picks, indent=2)}

    STRICT RULES:
    1. FACTUAL PITCHERS: NEVER invent or swap starting pitchers.
    2. BULLPEN FIDELITY: Respect the Season-Weighted Bullpen Status explicitly. If Python flags a closer on back-to-back usage, heavily penalize them.
    3. TIME CONTEXT: Utilize the provided 'start_time' to evaluate schedule fatigue.
    4. MARKET SELECTION: Balance selections across Moneylines, Run Lines, and Totals where edges exist.
    5. SPORTSBOOKS: FanDuel, DraftKings, BetMGM, Caesars ONLY.
    6. THE 11 PERCENT EV THRESHOLD (UNCAPPED): Evaluate every single matchup on the board. You MUST recommend every single play that calculates to an Expected Value (EV) of 11.0% or higher. There is NO CAP on the number of picks. If 10 games clear the 11.0% threshold, output all 10. If zero games clear it, output 0.
    7. MANDATORY VALIDATION: If 'ACTIVE PENDING PICKS' contains items, evaluate each against current odds. If the EV has dropped below 11.0% due to line movement or fatigue updates, output "REJECTED" for that pick. If it remains at or above 11.0%, output "VALIDATED". If 'ACTIVE PENDING PICKS' is empty, return an empty array (`"validations": []`).

    OUTPUT SCHEMA (STRICT JSON):
    {{
      "validations": [
        {{
          "row_index": <int matching row_index in open_picks>,
          "action": "VALIDATED" or "REJECTED",
          "updated_odds": <int or float, e.g. -110>,
          "updated_implied_prob": "52.4%",
          "updated_model_prob": "58.0%",
          "updated_expected_value": "+11.7%",
          "high_agreement": "<Consensus/Divergence>",
          "reason": "<tight summary>"
        }}
      ],
      "new_picks": [
        {{
          "date": "YYYY-MM-DD",
          "start_time": "YYYY-MM-DD HH:MM PM EDT",
          "game": "Away Team @ Home Team",
          "bet_type": "Moneyline (FanDuel)",
          "pick": "Team Name",
          "odds": -110,
          "implied_prob": "52.4%",
          "model_prob": "58.0%",
          "expected_value": "+11.7%",
          "high_agreement": "<Consensus/Divergence>",
          "reasoning": "<tight summary highlighting specific drivers including start times>"
        }}
      ]
    }}
    """

    candidate_models = [
        "gemini-3.1-pro-preview", 
        "gemini-3.7-flash"        
    ]

    for model_name in candidate_models:
        for attempt in range(2):
            try:
                print(f"Attempting synthesis with model: {model_name} (Attempt {attempt+1})...")
                response = client.models.generate_content(model=model_name, contents=prompt)
                parsed = parse_json_from_response(response)
                if parsed and ("new_picks" in parsed or "validations" in parsed):
                    print(f"Success! Model {model_name} generated valid JSON output.")
                    return parsed
            except errors.ClientError as e:
                print(f"ClientError on {model_name}: {e}")
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e): time.sleep(10)
                else: break
            except Exception as e:
                print(f"Unexpected error on {model_name}: {e}")
                break
    return {"validations": [], "new_picks": []}

# --- 9. MAIN EXECUTION ---
def main():
    spreadsheet, sheet = get_sheets()
    ensure_headers(sheet)
    ensure_evolution_sheet(spreadsheet)
    
    odds_key = os.environ.get("ODDS_API_KEY")
    graded_count = auto_grade_pending_bets(sheet, odds_key) if odds_key else 0
    update_scoreboard(spreadsheet)

    memory = load_memory()
    updated_memory = update_memory_from_sheet(sheet, memory)
    
    today_date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    current_time_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S EDT")
    
    update_evolution_log(spreadsheet, "MLB", updated_memory, f"Execution run. Graded {graded_count} bets.", current_time_str)

    probable_pitchers = fetch_today_probable_pitchers(today_date_str)
    print(f"Fetched {len(probable_pitchers)} probable starting pitcher(s).")
    
    fatigue_data = fetch_recent_bullpen_usage(days_back=2)
    odds = fetch_mlb_odds(odds_key)
    print(f"Fetched {len(odds)} game(s) from The Odds API.")
    
    if not odds:
        print("WARNING: No odds returned from API.")
        return

    open_picks = get_today_existing_picks(sheet, today_date_str)
    ai_response = generate_picks_and_validations(odds, updated_memory, open_picks, fatigue_data, probable_pitchers)
    
    validations = ai_response.get("validations", [])
    new_picks = ai_response.get("new_picks", [])
    
    if validations:
        print(f"Processing {len(validations)} pick validation(s)...")
        for val in validations:
            if not isinstance(val, dict):
                print(f"  [Warning] Skipping malformed validation: {val}")
                continue

            row_idx = val.get("row_index")
            action = str(val.get("action", "")).strip().upper()
            reason = str(val.get("reason", "")).strip()

            if row_idx and action in ["VALIDATED", "REJECTED"]:
                sheet.update_cell(row_idx, 14, action)
                if action == "VALIDATED":
                    updated_odds = val.get("updated_odds")
                    updated_model_prob = val.get("updated_model_prob")
                    if updated_odds: sheet.update_cell(row_idx, 6, int(round(float(updated_odds))))
                    if "updated_implied_prob" in val and val["updated_implied_prob"]: sheet.update_cell(row_idx, 7, val["updated_implied_prob"])
                    if updated_model_prob: sheet.update_cell(row_idx, 8, updated_model_prob)
                    if "updated_expected_value" in val and val["updated_expected_value"]: sheet.update_cell(row_idx, 9, val["updated_expected_value"])
                    if updated_odds and updated_model_prob:
                        qk_units = compute_quarter_kelly_units(updated_odds, updated_model_prob)
                        sheet.update_cell(row_idx, 10, qk_units)
                    if "high_agreement" in val and val["high_agreement"]: sheet.update_cell(row_idx, 15, str(val["high_agreement"]))
                    if reason: sheet.update_cell(row_idx, 13, reason)
                    sheet.update_cell(row_idx, 2, current_time_str)
                elif action == "REJECTED":
                    sheet.update_cell(row_idx, 11, "REJECTED")
                    sheet.update_cell(row_idx, 12, 0.0)
                    if reason: sheet.update_cell(row_idx, 13, reason)
                    sheet.update_cell(row_idx, 2, current_time_str)

                print(f"Row {row_idx} evaluated as {action}.")

    raw_rows = sheet.get_all_values()
    appended, skipped = 0, 0
    
    for p in new_picks:
        if not isinstance(p, dict):
            print(f"  [Warning] Skipping malformed pick entry: {p}")
            continue

        pick_date = str(p.get("date", today_date_str)).strip()
        start_time_out = str(p.get("start_time", "")).strip()
        game = str(p.get("game", "")).strip()
        bet_type = str(p.get("bet_type", "")).strip()
        pick = str(p.get("pick", "")).strip()
        reasoning = str(p.get("reasoning", "")).strip()
        model_prob_str = str(p.get("model_prob", "50.0%"))
        
        try: odds_val = float(p.get("odds", -110))
        except: odds_val = -110.0

        if game_already_pending(raw_rows, pick_date, game):
            print(f"Skipping duplicate game prediction: {game}")
            skipped += 1
            continue
            
        if not check_for_hallucinated_pitchers(game, reasoning, probable_pitchers):
            skipped += 1
            continue

        qk_units = compute_quarter_kelly_units(odds_val, model_prob_str)

        sheet.append_row([
            pick_date, current_time_str, game, bet_type, pick, int(round(odds_val)),
            p.get("implied_prob", ""), model_prob_str, p.get("expected_value", ""),
            qk_units, "PENDING", 0.0, reasoning, "NEW", p.get("high_agreement", "No"),
            start_time_out
        ], value_input_option="USER_ENTERED")
        appended += 1
            
    print(f"Execution complete! Added {appended} new pick(s). Skipped {skipped} duplicate(s)/hallucination(s).")

if __name__ == "__main__":
    main()
