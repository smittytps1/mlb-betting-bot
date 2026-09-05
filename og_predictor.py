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

def normalize_market_type(bet_type_str):
    lower = str(bet_type_str).lower()
    if "moneyline" in lower or "h2h" in lower:
        return "moneyline"
    elif "spread" in lower or "run line" in lower:
        return "run_line"
    elif "total" in lower or "over" in lower or "under" in lower:
        return "total"
    return lower.strip()

def american_to_decimal(odds):
    try:
        odds_f = float(odds)
        return (odds_f / 100.0) + 1.0 if odds_f > 0 else (100.0 / abs(odds_f)) + 1.0
    except Exception:
        return 1.91

def get_vig_free_probs(home_odds, away_odds):
    try:
        def implied(odds):
            val = float(odds)
            return abs(val) / (abs(val) + 100) if val < 0 else 100 / (val + 100)
        p_home = implied(home_odds)
        p_away = implied(away_odds)
        total = p_home + p_away
        if total == 0: return 0.50, 0.50
        return round(p_home / total, 4), round(p_away / total, 4)
    except Exception:
        return 0.50, 0.50

def compute_quarter_kelly_units(odds, model_prob_str):
    try:
        odds_val = float(odds)
        prob_val = float(str(model_prob_str).replace('%', '').strip()) / 100.0
        dec_odds = american_to_decimal(odds_val)
        b = dec_odds - 1.0
        if b <= 0: return 0.75
        kelly = (b * prob_val - (1.0 - prob_val)) / b
        if kelly <= 0: return 0.5
        
        raw_units = (kelly * 0.25) * 40.0
        
        if odds_val < 100:
            return max(0.5, min(1.00, round(raw_units, 2)))
        else:
            return max(0.5, min(1.25, round(raw_units, 2)))
    except Exception:
        return 0.75

# --- 1. GOOGLE SHEETS SETUP (OG PREDICTOR ISOLATION) ---
def get_sheets():
    print("Connecting to Google Sheets ('OG Predictor' Tab)...")
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    service_account_str = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    if not service_account_str: raise ValueError("GCP_SERVICE_ACCOUNT_JSON missing!")
    client = gspread.service_account_from_dict(json.loads(service_account_str), scopes=scopes)
    spreadsheet = client.open("MLB AI Betting Tracker")
    
    try:
        sheet = spreadsheet.worksheet("OG Predictor")
    except Exception:
        sheet = spreadsheet.add_worksheet(title="OG Predictor", rows=500, cols=20)
        
    return spreadsheet, sheet

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
        try: evo_sheet = spreadsheet.worksheet("OG Evolution & Learnings")
        except Exception: evo_sheet = spreadsheet.add_worksheet(title="OG Evolution & Learnings", rows=200, cols=10)
        if not evo_sheet.get_all_values():
            evo_sheet.insert_row(["Timestamp", "Sport", "Total Bets Evaluated", "Win Rate (%)", "Net Profit ($)", "Reasoning Factor Weights", "Active Strategy Adjustment", "Validation & Re-Synthesis Notes"], index=1)
        return evo_sheet
    except Exception: return None

def update_evolution_log(spreadsheet, sport_label, memory, summary, time_str):
    try:
        evo_sheet = ensure_evolution_sheet(spreadsheet)
        if not evo_sheet: return
        
        factors = memory.get("reasoning_factor_weights", {})
        weights_str = " | ".join([f"{k}: {v.get('weight', 1.0)}x" for k, v in factors.items()]) if factors else "Standard (1.0x)"

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

# --- 2. MULTI-SOURCE PROBABLES ---
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
    except Exception: pass

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
                                pitcher_map[canonical] = pitcher_info.get("fullName", "TBD")
    except Exception: pass

    return pitcher_map

def get_mlb_teams_map():
    resp = requests.get("https://statsapi.mlb.com/api/v1/teams?sportId=1", headers={"User-Agent": "Mozilla/5.0"})
    teams = {}
    if resp.status_code == 200:
        for t in resp.json().get("teams", []): teams[t["id"]] = match_canonical_team(t["name"])
    return teams

def fetch_high_leverage_relievers(teams_map):
    current_year = datetime.now(ZoneInfo("America/New_York")).year
    headers = {"User-Agent": "Mozilla/5.0"}
    leverage_weights = {}

    for team_id in teams_map.keys():
        try:
            url = f"https://statsapi.mlb.com/api/v1/stats?stats=season&group=pitching&playerPool=all&season={current_year}&teamId={team_id}&gameType=R"
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code != 200: continue
            stats_list = resp.json().get("stats", [])
            if not stats_list: continue

            relievers = []
            for split in stats_list[0].get("splits", []):
                pid = split.get("player", {}).get("id")
                stat = split.get("stat", {})
                games = int(stat.get("gamesPitched", 0))
                games_started = int(stat.get("gamesStarted", 0))

                if games > 0 and (games - games_started) >= 5:
                    relievers.append({
                        "id": pid,
                        "saves": int(stat.get("saves", 0)),
                        "holds": int(stat.get("holds", 0)),
                        "games_finished": int(stat.get("gamesFinished", 0))
                    })

            if not relievers: continue

            relievers_by_saves = sorted(relievers, key=lambda x: (x["saves"], x["games_finished"]), reverse=True)
            primary_closer = relievers_by_saves[0]
            if primary_closer["saves"] >= 2 or primary_closer["games_finished"] >= 5:
                leverage_weights[primary_closer["id"]] = 2.0

            remaining = [r for r in relievers if r["id"] != primary_closer["id"]]
            relievers_by_holds = sorted(remaining, key=lambda x: x["holds"], reverse=True)
            for setup_man in relievers_by_holds[:2]:
                if setup_man["holds"] >= 2:
                    leverage_weights[setup_man["id"]] = 1.5
        except Exception:
            continue
    return leverage_weights

def fetch_situational_fatigue_and_bullpen(days_back_bp=2, days_back_schedule=7):
    teams_map = get_mlb_teams_map()
    today = datetime.now(ZoneInfo("America/New_York")).date()
    headers = {"User-Agent": "Mozilla/5.0"}
    
    team_stats = {name: {"appearances": 0, "total_pitches": 0, "bp_dates": set(), "schedule_games_7d": 0, "high_lev_pitcher_dates": {}} for name in teams_map.values()}
    leverage_weights = fetch_high_leverage_relievers(teams_map)

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
                if canonical: team_stats[canonical]["schedule_games_7d"] += 1

            if d <= days_back_bp:
                box_resp = requests.get(f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore", headers=headers, timeout=10)
                if box_resp.status_code != 200: continue
                box_data = box_resp.json()
                for side in ["away", "home"]:
                    team_box = box_data.get("teams", {})[side]
                    canonical = teams_map.get(team_box.get("team", {}).get("id"))
                    if not canonical: continue
                    pitchers = team_box.get("pitchers", [])
                    players = team_box.get("players", {})
                    
                    if len(pitchers) > 1:
                        relief_pitcher_ids = pitchers[1:]
                        game_relief_pitches = 0
                        for pid in relief_pitcher_ids:
                            p_stats = players.get(f"ID{pid}", {}).get("stats", {}).get("pitching", {})
                            raw_pitches = int(p_stats.get("pitches", p_stats.get("numberOfPitches", 0)))
                            
                            weight = leverage_weights.get(pid, 1.0)
                            game_relief_pitches += (raw_pitches * weight)
                            
                            if weight >= 1.5:
                                if pid not in team_stats[canonical]["high_lev_pitcher_dates"]:
                                    team_stats[canonical]["high_lev_pitcher_dates"][pid] = set()
                                team_stats[canonical]["high_lev_pitcher_dates"][pid].add(target_date)
                                
                        if game_relief_pitches > 0:
                            team_stats[canonical]["total_pitches"] += game_relief_pitches
                            team_stats[canonical]["bp_dates"].add(target_date)
                            team_stats[canonical]["appearances"] += len(relief_pitcher_ids)

    objective_ratings = {}
    for team, stats in team_stats.items():
        total_p = stats["total_pitches"]
        load = round(float(total_p) / float(days_back_bp), 1) if total_p > 0 else 0.0
        
        if load >= 90.0:
            status = "TAXED"
        elif load >= 65.0:
            status = "MODERATELY WORKED"
        else:
            status = "FRESH"
            
        has_b2b_high_lev = any(len(dates) >= 2 for dates in stats["high_lev_pitcher_dates"].values())
            
        objective_ratings[team] = {
            "status_string": f"Status: {status} | Load Index: {load} | Relief Apps: {stats['appearances']} | Weighted Pitches (2 Days): {total_p} | Games Played (Last 7 Days): {stats['schedule_games_7d']}",
            "load": load,
            "closer_b2b": has_b2b_high_lev
        }
    return objective_ratings

# --- 4. ACCURATE AUTO-GRADING ---
def auto_grade_pending_bets(sheet, odds_key):
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1: return 0
        headers = [h.strip() for h in rows[0]]
        status_idx = headers.index("Status") if "Status" in headers else 10
        game_idx = headers.index("Game") if "Game" in headers else 2
        bet_type_idx = headers.index("Bet Type / Sportsbook") if "Bet Type / Sportsbook" in headers else 3
        pick_idx = headers.index("Pick") if "Pick" in headers else 4
        odds_idx = headers.index("Odds") if "Odds" in headers else 5
        units_idx = headers.index("Units") if "Units" in headers else 9
        
        pending_rows = [(i, r) for i, r in enumerate(rows[1:], start=2) if len(r) > status_idx and str(r[status_idx]).strip().upper() == "PENDING"]
        if not pending_rows: return 0

        resp = requests.get(f"https://api.the-odds-api.com/v4/sports/baseball_mlb/scores/?apiKey={odds_key}&daysFrom=3", timeout=10)
        if resp.status_code != 200: return 0
        scores_data = resp.json()
        updates = []

        for row_idx, r in pending_rows:
            try:
                pick_date_str = str(r[0]).strip()
                game_title = str(r[game_idx]).strip()
                bet_type = str(r[bet_type_idx]).strip().lower()
                pick_str = str(r[pick_idx]).strip()
                odds = float(r[odds_idx]) if r[odds_idx] else -110.0
                units = float(r[units_idx]) if r[units_idx] else 1.0

                for match in scores_data:
                    if not match.get("completed"): continue
                    match_date_ny_str = ""
                    if match.get("commence_time"):
                        try:
                            match_date_ny_str = datetime.fromisoformat(match.get("commence_time").replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
                        except Exception: pass
                    
                    if pick_date_str != match_date_ny_str: continue
                    home_team, away_team = match.get("home_team", ""), match.get("away_team", "")
                    
                    if match_canonical_team(home_team) in game_title or match_canonical_team(away_team) in game_title:
                        scores = match.get("scores")
                        if not scores or len(scores) < 2: continue
                        home_score = next((int(s["score"]) for s in scores if s["name"] == home_team), 0)
                        away_score = next((int(s["score"]) for s in scores if s["name"] == away_team), 0)
                        total_score = home_score + away_score
                        
                        status = "PENDING"
                        
                        if "moneyline" in bet_type or "h2h" in bet_type:
                            winner = home_team if home_score > away_score else away_team
                            status = "WIN" if match_canonical_team(pick_str).lower() == match_canonical_team(winner).lower() else "LOSS"
                            
                        elif "spread" in bet_type or "run line" in bet_type:
                            spread_match = re.search(r'([-+]\s*\d+\.?\d*)', pick_str) or re.search(r'([-+]\s*\d+\.?\d*)', bet_type)
                            spread_val = float(spread_match.group(1).replace(" ", "")) if spread_match else 0.0
                            
                            clean_pick_team = re.sub(r'[-+]\s*\d+\.?\d*', '', pick_str).strip()
                            is_home = match_canonical_team(clean_pick_team).lower() == match_canonical_team(home_team).lower()
                            
                            diff = (home_score + spread_val) - away_score if is_home else (away_score + spread_val) - home_score
                            if diff == 0: status = "PUSH"
                            elif diff > 0: status = "WIN"
                            else: status = "LOSS"
                            
                        elif "total" in bet_type or "over" in bet_type or "under" in bet_type or "over" in pick_str.lower() or "under" in pick_str.lower():
                            num_match = re.search(r'([0-9]+\.?[0-9]*)', pick_str) or re.search(r'([0-9]+\.?[0-9]*)', bet_type)
                            line_val = float(num_match.group(1)) if num_match else 0.0
                            is_over = "over" in pick_str.lower() or "over" in bet_type
                            
                            if total_score == line_val: status = "PUSH"
                            elif (is_over and total_score > line_val) or (not is_over and total_score < line_val): status = "WIN"
                            else: status = "LOSS"

                        if status in ["WIN", "LOSS", "PUSH"]:
                            profit = 0.0
                            if status == "WIN":
                                profit = ((odds / 100.0) * 100.0 * units) if odds > 0 else ((100.0 / abs(odds)) * 100.0 * units)
                            elif status == "LOSS":
                                profit = -100.0 * units
                            updates.append({"range": f"K{row_idx}:L{row_idx}", "values": [[status, round(profit, 2)]]})
                        break
            except Exception as row_err:
                print(f"Error grading row {row_idx}: {row_err}")
                continue
        
        if updates:
            sheet.batch_update(updates)
            print(f"Successfully auto-graded {len(updates)} pending bet(s).")
        return len(updates)
    except Exception as e:
        print(f"Auto-grade batch notice: {e}")
        return 0

# --- 5. SCOREBOARD ENGINE ---
def update_scoreboard(spreadsheet):
    try:
        try: sb = spreadsheet.worksheet("OG Scoreboard")
        except: sb = spreadsheet.add_worksheet(title="OG Scoreboard", rows=20, cols=10)
        
        scoreboard_data = [
            ["Bot / Sport & Timeframe", "Correct Picks (Wins)", "Incorrect Picks (Losses)", "Pending Bets", "Win Rate (%)", "Total Money Won / Lost ($)"],
            ["OG Predictor (All-Time)", '=COUNTIF(\'OG Predictor\'!K:K, "WIN")', '=COUNTIF(\'OG Predictor\'!K:K, "LOSS")', '=COUNTIF(\'OG Predictor\'!K:K, "PENDING")', '=IFERROR(B2/(B2+C2), 0)', '=SUM(\'OG Predictor\'!L:L)'],
            ["OG Predictor (7-Day)", '=SUMPRODUCT((\'OG Predictor\'!K2:K="WIN")*(IFERROR(DATEVALUE(\'OG Predictor\'!A2:A),IFERROR(VALUE(\'OG Predictor\'!A2:A),0))>=TODAY()-7)*(\'OG Predictor\'!A2:A<>""))', '=SUMPRODUCT((\'OG Predictor\'!K2:K="LOSS")*(IFERROR(DATEVALUE(\'OG Predictor\'!A2:A),IFERROR(VALUE(\'OG Predictor\'!A2:A),0))>=TODAY()-7)*(\'OG Predictor\'!A2:A<>""))', '=SUMPRODUCT((\'OG Predictor\'!K2:K="PENDING")*(IFERROR(DATEVALUE(\'OG Predictor\'!A2:A),IFERROR(VALUE(\'OG Predictor\'!A2:A),0))>=TODAY()-7)*(\'OG Predictor\'!A2:A<>""))', '=IFERROR(B3/(B3+C3), 0)', '=SUMPRODUCT((IFERROR(DATEVALUE(\'OG Predictor\'!A2:A),IFERROR(VALUE(\'OG Predictor\'!A2:A),0))>=TODAY()-7)*(\'OG Predictor\'!A2:A<>"")*(IFERROR(VALUE(\'OG Predictor\'!L2:L),0)))'],
            ["OG Predictor (3-Day)", '=SUMPRODUCT((\'OG Predictor\'!K2:K="WIN")*(IFERROR(DATEVALUE(\'OG Predictor\'!A2:A),IFERROR(VALUE(\'OG Predictor\'!A2:A),0))>=TODAY()-3)*(\'OG Predictor\'!A2:A<>""))', '=SUMPRODUCT((\'OG Predictor\'!K2:K="LOSS")*(IFERROR(DATEVALUE(\'OG Predictor\'!A2:A),IFERROR(VALUE(\'OG Predictor\'!A2:A),0))>=TODAY()-3)*(\'OG Predictor\'!A2:A<>""))', '=SUMPRODUCT((\'OG Predictor\'!K2:K="PENDING")*(IFERROR(DATEVALUE(\'OG Predictor\'!A2:A),IFERROR(VALUE(\'OG Predictor\'!A2:A),0))>=TODAY()-3)*(\'OG Predictor\'!A2:A<>""))', '=IFERROR(B4/(B4+C4), 0)', '=SUMPRODUCT((IFERROR(DATEVALUE(\'OG Predictor\'!A2:A),IFERROR(VALUE(\'OG Predictor\'!A2:A),0))>=TODAY()-3)*(\'OG Predictor\'!A2:A<>"")*(IFERROR(VALUE(\'OG Predictor\'!L2:L),0)))'],
            ["OG Predictor (1-Day)", '=SUMPRODUCT((\'OG Predictor\'!K2:K="WIN")*(IFERROR(DATEVALUE(\'OG Predictor\'!A2:A),IFERROR(VALUE(\'OG Predictor\'!A2:A),0))>=TODAY()-1)*(\'OG Predictor\'!A2:A<>""))', '=SUMPRODUCT((\'OG Predictor\'!K2:K="LOSS")*(IFERROR(DATEVALUE(\'OG Predictor\'!A2:A),IFERROR(VALUE(\'OG Predictor\'!A2:A),0))>=TODAY()-1)*(\'OG Predictor\'!A2:A<>""))', '=SUMPRODUCT((\'OG Predictor\'!K2:K="PENDING")*(IFERROR(DATEVALUE(\'OG Predictor\'!A2:A),IFERROR(VALUE(\'OG Predictor\'!A2:A),0))>=TODAY()-1)*(\'OG Predictor\'!A2:A<>""))', '=IFERROR(B5/(B5+C5), 0)', '=SUMPRODUCT((IFERROR(DATEVALUE(\'OG Predictor\'!A2:A),IFERROR(VALUE(\'OG Predictor\'!A2:A),0))>=TODAY()-1)*(\'OG Predictor\'!A2:A<>"")*(IFERROR(VALUE(\'OG Predictor\'!L2:L),0)))']
        ]
        sb.update(range_name="A1:F5", values=scoreboard_data, value_input_option="USER_ENTERED")
    except Exception as e:
        print(f"Scoreboard notice: {e}")

# --- 6. RECURSIVE MEMORY & FACTOR WEIGHTING ---
def load_memory():
    if os.path.exists("og_memory.json"):
        try:
            with open("og_memory.json", "r") as f: return json.load(f)
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
    with open("og_memory.json", "w") as f:
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
                        if any(kw in reasoning for factor_key in kws):
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
        
        with open("og_memory.json", "w") as f: json.dump(memory, f, indent=2)
    except Exception: pass
    return memory

# --- 7. MATCHUP FORMATTING & STRICT TEMPORAL GUARDRAILS ---
def fetch_mlb_odds(odds_key):
    resp = requests.get(f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/?apiKey={odds_key}&regions=us&markets=h2h,spreads,totals&oddsFormat=american")
    return resp.json() if resp.status_code == 200 else []

def get_today_existing_picks(sheet, today_date_str):
    rows = sheet.get_all_values()
    if len(rows) <= 1: return []
    return [{"row_index": i, "date": r[0], "game": r[2], "status": r[10]} for i, r in enumerate(rows[1:], start=2) if r[0] == today_date_str and r[10] == "PENDING"]

def check_for_hallucinated_pitchers(game_str, reasoning_str, probable_pitchers):
    try:
        parts = game_str.split("@")
        if len(parts) != 2: return True
        away_canonical, home_canonical = match_canonical_team(parts[0].strip()), match_canonical_team(parts[1].strip())
        
        for team, pitcher_name in probable_pitchers.items():
            if team == home_canonical or team == away_canonical: continue
            foreign_pitcher_name = pitcher_name.split("(")[0].strip()
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

        home_odds_val, away_odds_val = -110, -110
        bookmakers = game.get("bookmakers", [])
        if bookmakers:
            for book in bookmakers:
                for market in book.get("markets", []):
                    if market.get("key") == "h2h":
                        for outcome in market.get("outcomes", []):
                            team_name = match_canonical_team(outcome.get("name", ""))
                            if team_name == home: home_odds_val = outcome.get("price")
                            elif team_name == away: away_odds_val = outcome.get("price")
                        break
                if home_odds_val != -110 or away_odds_val != -110:
                    break
        
        market_home_prob, market_away_prob = get_vig_free_probs(home_odds_val, away_odds_val)
            
        away_bp_str = objective_fatigue_ratings.get(away, {}).get("status_string", "Status: FRESH | Load Index: 0.0")
        home_bp_str = objective_fatigue_ratings.get(home, {}).get("status_string", "Status: FRESH | Load Index: 0.0")

        game_copy = dict(game)
        game_copy["matchup_context"] = {
            "start_time": game_time_et,
            "away": f"{away} | Starter: {a_pitcher} | Market Base Prob: {round(market_away_prob*100, 1)}% | Bullpen: {away_bp_str}",
            "home": f"{home} | Starter: {h_pitcher} | Market Base Prob: {round(market_home_prob*100, 1)}% | Bullpen: {home_bp_str}"
        }
        valid.append(game_copy)
        
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

    === TODAY'S MATCHUPS & VIG-FREE MARKET PROBABILITIES ===
    {json.dumps(formatted_games, indent=2)}

    === ACTIVE PENDING PICKS ===
    {json.dumps(open_picks, indent=2)}

    STRICT RULES:
    1. MARKET PROBABILITY ANCHOR: You MUST anchor all probability evaluations to the provided 'Market Base Prob'. Do NOT evaluate massive underdogs (+160 or higher) as 50/50 coin flips. Maximum allowable shift from the Market Base Prob is ±7.0%.
    2. FACTUAL PITCHERS: NEVER invent or swap starting pitchers. Ground analysis in confirmed starters.
    3. BULLPEN FIDELITY: Respect the Season-Weighted Bullpen Status explicitly. If Python flags a closer on back-to-back usage, penalize them appropriately, but do not let bullpen fatigue completely override elite starting pitchers.
    4. SPORTSBOOKS: Pick ONLY from: {ALLOWED_SPORTSBOOKS}.
    5. STRICT TOP-5 EV CAP: Evaluate every matchup on the board. Recommend ONLY the highest-value plays that calculate to an Expected Value (EV) of 11.0% or higher. You must NEVER output more than 5 total picks per run.
    6. MANDATORY VALIDATION: If 'ACTIVE PENDING PICKS' contains items, evaluate each against current odds. If the EV has dropped below 11.0%, output "REJECTED". If it remains at or above 11.0%, output "VALIDATED".
    7. TOTALS REQUIREMENT: All recommended Over/Under Totals MUST possess an Expected Value of 12.0% or higher. Do not output borderline totals.
    8. NO SPREAD/TOTAL COMBOS: Never pick parlay-style outcomes. Stick to single-market Moneyline, Run Line, or Total selections.
    9. SPREAD / RUN LINE FORMATTING: If picking a Run Line, you MUST place the spread value inside the 'pick' field (e.g., "pick": "Atlanta Braves -1.5") and keep the bet_type clean (e.g., "bet_type": "Run Line (FanDuel)").
    10. MATCHING PICK TO REASONING: The team named in the 'pick' field MUST perfectly match the team favored in the 'reasoning' field. Never accidentally output the wrong team.

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

    candidate_models = ["gemini-3.1-pro-preview", "gemini-3.7-flash"]

    for model_name in candidate_models:
        for attempt in range(2):
            try:
                response = client.models.generate_content(model=model_name, contents=prompt)
                parsed = parse_json_from_response(response)
                if parsed and ("new_picks" in parsed or "validations" in parsed):
                    return parsed
            except errors.ClientError as e:
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e): time.sleep(10)
                else: break
            except Exception:
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
    
    update_evolution_log(spreadsheet, "MLB (OG)", updated_memory, f"Execution run. Graded {graded_count} bets.", current_time_str)

    probable_pitchers = fetch_today_probable_pitchers(today_date_str)
    
    # Using the new, accurate, normalized bullpen load metric
    fatigue_data = fetch_situational_fatigue_and_bullpen(days_back_bp=2, days_back_schedule=7)
    
    odds = fetch_mlb_odds(odds_key)
    if not odds: return

    open_picks = get_today_existing_picks(sheet, today_date_str)
    ai_response = generate_picks_and_validations(odds, updated_memory, open_picks, fatigue_data, probable_pitchers)
    
    validations = ai_response.get("validations", [])
    new_picks = ai_response.get("new_picks", [])
    
    if validations:
        for val in validations:
            if not isinstance(val, dict): continue

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

    existing_rows = sheet.get_all_values()
    existing_market_signatures = set()
    if len(existing_rows) > 1:
        for r in existing_rows[1:]:
            if len(r) > 10 and str(r[10]).strip().upper() == "PENDING":
                game_sig = str(r[2]).strip()
                market_sig = normalize_market_type(r[3])
                existing_market_signatures.add(f"{game_sig} | {market_sig}")

    def parse_ev(item):
        try: return float(str(item.get("expected_value", "0")).replace("%", "").replace("+", "").strip())
        except Exception: return 0.0

    valid_new_picks = []
    for p in new_picks:
        if not isinstance(p, dict): continue
        game = str(p.get("game", "")).strip()
        bet_type = str(p.get("bet_type", "")).strip()
        market_norm = normalize_market_type(bet_type)
        market_signature = f"{game} | {market_norm}"
        reasoning = str(p.get("reasoning", "")).strip()
        ev_val = parse_ev(p)

        if market_signature in existing_market_signatures: continue
        if not check_for_hallucinated_pitchers(game, reasoning, probable_pitchers): continue

        # Hard Python Guardrail: Filter Totals under 12.0% EV
        if market_norm == "total" and ev_val < 12.0: continue

        valid_new_picks.append(p)

    # Strictly take top 5 highest EV picks
    top_5_picks = sorted(valid_new_picks, key=parse_ev, reverse=True)[:5]
    
    for p in top_5_picks:
        pick_date = str(p.get("date", today_date_str)).strip()
        start_time_out = str(p.get("start_time", "")).strip()
        game = str(p.get("game", "")).strip()
        bet_type = str(p.get("bet_type", "")).strip()
        pick = str(p.get("pick", "")).strip()
        reasoning = str(p.get("reasoning", "")).strip()
        model_prob_str = str(p.get("model_prob", "50.0%"))
        
        try: odds_val = float(p.get("odds", -110))
        except: odds_val = -110.0

        qk_units = compute_quarter_kelly_units(odds_val, model_prob_str)

        sheet.append_row([
            pick_date, current_time_str, game, bet_type, pick, int(round(odds_val)),
            p.get("implied_prob", ""), model_prob_str, p.get("expected_value", ""),
            qk_units, "PENDING", 0.0, reasoning, "NEW", p.get("high_agreement", "No"),
            start_time_out
        ], value_input_option="USER_ENTERED")
        
        existing_market_signatures.add(f"{game} | {normalize_market_type(bet_type)}")

if __name__ == "__main__":
    main()
