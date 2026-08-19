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
    if not name_str:
        return ""
    cleaned = str(name_str).strip().lower()
    cleaned_norm = normalize_text(cleaned)

    for canonical, aliases in MLB_TEAM_ALIASES.items():
        for alias in aliases:
            if alias == cleaned or normalize_text(alias) == cleaned_norm or alias in cleaned or cleaned in alias:
                return canonical.title()
    return name_str.strip().title()

# --- 1. GOOGLE SHEETS AUTHENTICATION & SETUP ---
def get_sheets():
    print("Connecting to Google Sheets (MLB Tab)...")
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    service_account_str = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    if not service_account_str:
        raise ValueError("GCP_SERVICE_ACCOUNT_JSON environment variable is missing!")
    
    creds_dict = json.loads(service_account_str)
    client = gspread.service_account_from_dict(creds_dict, scopes=scopes)
    
    spreadsheet = client.open("MLB AI Betting Tracker")
    sheet = spreadsheet.worksheet("MLB")
    return spreadsheet, sheet

def ensure_headers(sheet):
    """Ensures row 1 contains all 15 column headers including High Agreement and Validation."""
    try:
        existing_rows = sheet.get_all_values()
        headers = [
            "Date", "Pulled Time", "Game", "Bet Type / Sportsbook", "Pick", "Odds", 
            "Implied Prob (%)", "Model Prob (%)", "EV (%)", "Units", 
            "Status", "P/L ($)", "Reasoning", "Validation", "High Agreement & Source Breakdown"
        ]

        if not existing_rows or not existing_rows[0] or existing_rows[0][0] != "Date":
            print("Writing MLB column headers to row 1...")
            sheet.insert_row(headers, index=1)
        else:
            current_row_len = len(existing_rows[0])
            if current_row_len < 14 or existing_rows[0][13] != "Validation":
                sheet.update_cell(1, 14, "Validation")
            if current_row_len < 15 or (current_row_len >= 15 and "High Agreement" not in existing_rows[0][14]):
                sheet.update_cell(1, 15, "High Agreement & Source Breakdown")
    except Exception as e:
        print(f"Header formatting notice: {e}")

def ensure_evolution_sheet(spreadsheet):
    """Safely guarantees the 'Evolution & Learnings' tab exists and has headers."""
    try:
        try:
            evo_sheet = spreadsheet.worksheet("Evolution & Learnings")
        except Exception:
            print("Creating 'Evolution & Learnings' worksheet tab...")
            evo_sheet = spreadsheet.add_worksheet(title="Evolution & Learnings", rows=200, cols=10)

        existing_rows = evo_sheet.get_all_values()
        headers = [
            "Timestamp", "Sport", "Total Bets Evaluated", "Win Rate (%)", 
            "Net Profit ($)", "Reasoning Factor Weights", "Active Strategy Adjustment", "Validation & Re-Synthesis Notes"
        ]

        if not existing_rows or len(existing_rows) == 0 or len(existing_rows[0]) == 0 or existing_rows[0][0] != "Timestamp":
            print("Writing headers to 'Evolution & Learnings' tab...")
            evo_sheet.insert_row(headers, index=1)

        return evo_sheet
    except Exception as e:
        print(f"Notice while ensuring Evolution sheet: {e}")
        return None

def update_evolution_log(spreadsheet, sport_label, memory, validations_summary, current_time_str):
    """Logs a live snapshot of factor weights, learning reflections, and adjustments."""
    try:
        evo_sheet = ensure_evolution_sheet(spreadsheet)
        if not evo_sheet:
            return

        factors = memory.get("reasoning_factor_weights", {})
        weights_str = " | ".join([f"{k}: {v.get('weight', 1.0)}x" for k, v in factors.items()])

        evo_sheet.append_row([
            current_time_str,
            sport_label,
            memory.get("total_bets", 0),
            memory.get("win_rate", "0%"),
            memory.get("net_profit_dollars", 0.0),
            weights_str if weights_str else "Standard (1.0x)",
            memory.get("learnings_and_adjustments", "Maintain standard criteria."),
            validations_summary if validations_summary else "Execution logged."
        ])
        print(f"Evolution & Learnings tab updated successfully for {sport_label}!")
    except Exception as e:
        print(f"Notice while logging to Evolution tab: {e}")

# --- 2. OFFICIAL MLB STATS API: TODAY'S PROBABLE PITCHERS ---
def fetch_today_probable_pitchers(target_date_str):
    """Fetches confirmed starting pitchers and their season stat baselines directly from MLB Stats API."""
    print(f"Fetching confirmed starting pitchers for {target_date_str} from MLB Stats API...")
    pitcher_map = {}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={target_date_str}&hydrate=probablePitcher(stats)"

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            dates = data.get("dates", [])
            if dates:
                for game in dates[0].get("games", []):
                    teams = game.get("teams", {})
                    for side in ["away", "home"]:
                        team_data = teams.get(side, {})
                        raw_team = team_data.get("team", {}).get("name", "")
                        canonical_team = match_canonical_team(raw_team)
                        
                        pitcher_info = team_data.get("probablePitcher", {})
                        if pitcher_info:
                            p_name = pitcher_info.get("fullName", "TBD")
                            p_hand = pitcher_info.get("pitchHand", {}).get("code", "R") + "HP"
                            
                            stats_list = pitcher_info.get("stats", [])
                            era = "N/A"
                            w_l = "0-0"
                            so = 0
                            bb = 0
                            ip = "0.0"
                            whip = "N/A"
                            
                            if stats_list:
                                splits = stats_list[0].get("splits", [])
                                if splits:
                                    stat_dict = splits[0].get("stat", {})
                                    era = stat_dict.get("era", "N/A")
                                    w_l = f"{stat_dict.get('wins', 0)}-{stat_dict.get('losses', 0)}"
                                    so = stat_dict.get("strikeOuts", 0)
                                    bb = stat_dict.get("baseOnBalls", 0)
                                    ip = stat_dict.get("inningsPitched", "0.0")
                                    whip = stat_dict.get("whip", "N/A")

                            pitcher_summary = f"{p_name} ({p_hand}, W-L: {w_l}, ERA: {era}, WHIP: {whip}, {so} SO, {bb} BB in {ip} IP)"
                            pitcher_map[canonical_team] = pitcher_summary
                        else:
                            pitcher_map[canonical_team] = "Starter TBD / Bullpen Day"
    except Exception as e:
        print(f"Notice fetching probable pitchers: {e}")

    for t_name, p_sum in pitcher_map.items():
        print(f"  [Probable Pitcher] {t_name}: {p_sum}")

    return pitcher_map

# --- 3. OFFICIAL MLB STATS API: DIRECT BOX SCORE INGESTION ---
def fetch_recent_bullpen_usage(days_back=2):
    """Fetches verified reliever pitch counts and box scores from MLB's official Stats API."""
    print(f"Fetching official MLB box scores & bullpen logs for last {days_back} day(s)...")
    bullpen_logs = {}
    today = datetime.now(ZoneInfo("America/New_York")).date()
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    for d in range(1, days_back + 1):
        target_date = (today - timedelta(days=d)).strftime("%Y-%m-%d")
        schedule_url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={target_date}"
        
        try:
            sched_resp = requests.get(schedule_url, headers=headers, timeout=10)
            if sched_resp.status_code != 200:
                continue
            
            sched_data = sched_resp.json()
            dates = sched_data.get("dates", [])
            if not dates:
                continue

            games = dates[0].get("games", [])
            print(f"Found {len(games)} game(s) on {target_date}. Parsing pitch logs...")

            for game in games:
                status = game.get("status", {}).get("abstractGameState")
                game_pk = game.get("gamePk")
                if status != "Final" or not game_pk:
                    continue

                box_url = f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore"
                try:
                    box_resp = requests.get(box_url, headers=headers, timeout=10)
                    if box_resp.status_code != 200:
                        continue
                    box_data = box_resp.json()
                except Exception:
                    continue

                teams_box = box_data.get("teams", {})
                for side in ["away", "home"]:
                    team_box = teams_box.get(side, {})
                    raw_team_name = team_box.get("team", {}).get("name", "")
                    canonical_name = match_canonical_team(raw_team_name)
                    if not canonical_name:
                        continue

                    opp_side = "home" if side == "away" else "away"
                    opp_raw = teams_box.get(opp_side, {}).get("team", {}).get("name", "")
                    opp_canonical = match_canonical_team(opp_raw)

                    pitchers = team_box.get("pitchers", [])
                    players = team_box.get("players", {})

                    if canonical_name not in bullpen_logs:
                        bullpen_logs[canonical_name] = []

                    if len(pitchers) > 1:
                        relievers_used = []
                        for pid in pitchers[1:]:
                            p_key = f"ID{pid}"
                            p_info = players.get(p_key, {})
                            p_name = p_info.get("person", {}).get("fullName", "Reliever")
                            p_stats = p_info.get("stats", {}).get("pitching", {})
                            pitches = p_stats.get("pitches", p_stats.get("numberOfPitches", 0))
                            ip = p_stats.get("inningsPitched", "0.0")
                            relievers_used.append(f"{p_name} ({ip} IP, {pitches} P)")

                        entry_summary = f"Played {target_date} vs {opp_canonical}: Used {len(relievers_used)} relievers -> [{', '.join(relievers_used)}]"
                        bullpen_logs[canonical_name].append(entry_summary)
                    else:
                        entry_summary = f"Played {target_date} vs {opp_canonical}: Starter threw complete game (0 relievers used)"
                        bullpen_logs[canonical_name].append(entry_summary)

        except Exception as e:
            print(f"Notice fetching MLB schedule for {target_date}: {e}")

    return bullpen_logs

# --- 4. ACCURATE AUTO-GRADING VIA SCORES API ---
def auto_grade_pending_bets(sheet, odds_key):
    """Grades PENDING bets accurately using dynamic header indexing."""
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1:
            return 0

        headers = [h.strip() for h in rows[0]]
        
        try:
            status_idx = headers.index("Status")
            game_idx = headers.index("Game")
            bet_type_idx = headers.index("Bet Type / Sportsbook")
            pick_idx = headers.index("Pick")
            pulled_idx = headers.index("Pulled Time")
            odds_idx = headers.index("Odds")
            units_idx = headers.index("Units")
        except ValueError as e:
            print(f"Auto-grading skipped: Missing header - {e}")
            return 0

        pending_rows = []
        for row_idx, r in enumerate(rows[1:], start=2):
            if len(r) > status_idx and str(r[status_idx]).strip().upper() == "PENDING":
                pending_rows.append((row_idx, r))

        if not pending_rows:
            print("No pending MLB bets to grade.")
            return 0

        print(f"Checking results for {len(pending_rows)} pending MLB bet(s)...")
        scores_url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/scores/?apiKey={odds_key}&daysFrom=3"
        resp = requests.get(scores_url)
        if resp.status_code != 200:
            print(f"Could not fetch MLB score data. Status code: {resp.status_code}")
            return 0

        scores_data = resp.json()
        updates = []

        for row_idx, r in pending_rows:
            game_title = str(r[game_idx]).strip()
            bet_type = str(r[bet_type_idx]).strip().lower()
            pick_str = str(r[pick_idx]).strip()
            pulled_time_raw = str(r[pulled_idx]).strip()
            
            try: odds = float(r[odds_idx])
            except (ValueError, TypeError): odds = -110.0

            try: units = float(r[units_idx]) if len(r) > units_idx and r[units_idx] else 1.0
            except (ValueError, TypeError): units = 1.0

            pulled_dt = None
            try:
                clean_time = pulled_time_raw.replace(" EDT", "").replace(" EST", "").strip()
                pulled_dt = datetime.strptime(clean_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("America/New_York"))
            except Exception:
                pass

            for match in scores_data:
                if not match.get("completed"):
                    continue

                home_team = match.get("home_team", "")
                away_team = match.get("away_team", "")
                commence_time_str = match.get("commence_time", "")

                home_canonical = match_canonical_team(home_team)
                away_canonical = match_canonical_team(away_team)

                if (home_canonical in game_title or away_canonical in game_title or
                    home_team in game_title or away_team in game_title):
                    if commence_time_str and pulled_dt:
                        try:
                            game_dt = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
                            if game_dt <= pulled_dt:
                                continue
                        except Exception:
                            pass

                    scores = match.get("scores")
                    if not scores or len(scores) < 2:
                        continue

                    home_score = next((int(s["score"]) for s in scores if s["name"] == home_team), 0)
                    away_score = next((int(s["score"]) for s in scores if s["name"] == away_team), 0)
                    total_score = home_score + away_score

                    status = None
                    profit = 0.0

                    pick_lower = pick_str.lower()
                    is_total_market = ("total" in bet_type or "over" in pick_lower or "under" in pick_lower or "o/u" in pick_lower)

                    # 1. TOTALS
                    if is_total_market:
                        num_match = re.search(r'(?:over|under|o/u|u|o)?\s*([0-9]+\.?[0-9]*)', pick_lower)
                        if num_match:
                            total_line = float(num_match.group(1))
                            is_over = bool(re.search(r'\b(over|o)\b', pick_lower))
                            is_under = bool(re.search(r'\b(under|u)\b', pick_lower))
                            if not is_over and not is_under:
                                is_over = "over" in pick_lower

                            if total_score == total_line:
                                status = "PUSH"
                                profit = 0.0
                            elif (is_over and total_score > total_line) or (is_under and total_score < total_line):
                                status = "WIN"
                            else:
                                status = "LOSS"

                    # 2. RUN LINES / SPREADS
                    elif "spread" in bet_type or "run line" in bet_type or re.search(r'[-+]\d+\.?\d*', pick_str):
                        spread_match = re.search(r'([-+]\s*\d+\.?\d*)', pick_str)
                        spread_val = float(spread_match.group(1).replace(" ", "")) if spread_match else 0.0
                        
                        is_home_pick = (home_canonical.lower() in pick_lower or home_team.lower() in pick_lower)
                        pick_score = home_score if is_home_pick else away_score
                        opp_score = away_score if is_home_pick else home_score

                        diff = (pick_score + spread_val) - opp_score
                        if diff == 0:
                            status = "PUSH"
                            profit = 0.0
                        elif diff > 0:
                            status = "WIN"
                        else:
                            status = "LOSS"

                    # 3. MONEYLINE
                    else:
                        winner = home_team if home_score > away_score else away_team
                        winner_canonical = match_canonical_team(winner)
                        pick_canonical = match_canonical_team(pick_str)

                        is_win = (pick_canonical.lower() == winner_canonical.lower() or 
                                  pick_lower in winner.lower() or 
                                  winner.lower() in pick_lower)
                        status = "WIN" if is_win else "LOSS"

                    if status == "WIN":
                        profit = (100 / abs(odds)) * 100 * units if odds < 0 else (odds / 100) * 100 * units
                    elif status == "LOSS":
                        profit = -100.0 * units
                    elif status == "PUSH":
                        profit = 0.0

                    print(f"Graded Row {row_idx}: {game_title} [{pick_str}] -> {status} (${round(profit, 2)})")

                    updates.append({
                        "range": f"K{row_idx}:L{row_idx}",
                        "values": [[status, round(profit, 2)]]
                    })
                    break

        if updates:
            print(f"Batch updating {len(updates)} row(s) in MLB tab...")
            sheet.batch_update(updates)
            print("Successfully auto-graded pending MLB bets!")
            return len(updates)

    except Exception as e:
        print(f"Auto-grading completed with notice: {e}")
    return 0

# --- 5. SCOREBOARD UPDATER ---
def update_scoreboard(spreadsheet):
    try:
        try:
            sb_sheet = spreadsheet.worksheet("Scoreboard")
        except Exception:
            sb_sheet = spreadsheet.add_worksheet(title="Scoreboard", rows=20, cols=10)

        scoreboard_data = [
            ["Bot / Sport", "Correct Picks (Wins)", "Incorrect Picks (Losses)", "Pending Bets", "Win Rate (%)", "Total Money Won / Lost ($)"],
            ["MLB Bot", '=COUNTIF(MLB!K:K, "WIN")', '=COUNTIF(MLB!K:K, "LOSS")', '=COUNTIF(MLB!K:K, "PENDING")', '=IFERROR(B2/(B2+C2), 0)', '=SUM(MLB!L:L)'],
            ["WNBA Bot", '=COUNTIF(WNBA!K:K, "WIN")', '=COUNTIF(WNBA!K:K, "LOSS")', '=COUNTIF(WNBA!K:K, "PENDING")', '=IFERROR(B3/(B3+C3), 0)', '=SUM(WNBA!L:L)'],
            ["Total Overall", '=B2+B3', '=C2+C3', '=D2+D3', '=IFERROR(B4/(B4+C4), 0)', '=F2+F3']
        ]

        sb_sheet.update(range_name="A1:F4", values=scoreboard_data, value_input_option="USER_ENTERED")
        print("Scoreboard tab successfully updated with live formulas!")
    except Exception as e:
        print(f"Notice while updating Scoreboard: {e}")

# --- 6. RECURSIVE MEMORY & FACTOR WEIGHTING ---
def load_memory():
    if os.path.exists("bot_memory.json"):
        try:
            with open("bot_memory.json", "r") as f:
                return json.load(f)
        except Exception:
            pass
    
    default_memory = {
        "total_bets": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": "0%",
        "net_profit_dollars": 0.0,
        "high_agreement_yes_performance": {"wins": 0, "losses": 0, "win_rate": "0%", "profit": 0.0},
        "high_agreement_no_performance": {"wins": 0, "losses": 0, "win_rate": "0%", "profit": 0.0},
        "learnings_and_adjustments": "Maintain balanced quantitative multi-factor evaluation across FanGraphs, Statcast, Ballpark Pal, and multi-model consensus synthesis.",
        "reasoning_factor_weights": {
            "starting_pitcher_expected_metrics": {
                "wins": 0, "losses": 0, "weight": 1.0, 
                "instruction": "Evaluate FIP, xFIP, SIERA, xERA, K-BB%, CSW%, Stuff+, Location+, and pitch arsenal Run Values (RV/100)."
            },
            "platoon_and_lineup_splits": {
                "wins": 0, "losses": 0, "weight": 1.0, 
                "instruction": "Evaluate wRC+ and OPS splits vs LHP/RHP, confirmed daily starting lineups, and key rest spots."
            },
            "statcast_contact_quality": {
                "wins": 0, "losses": 0, "weight": 1.0, 
                "instruction": "Evaluate xwOBA, Hard-Hit%, Barrel%, xBA, and xSLG to identify lucky BABIP anomalies vs genuine authority."
            },
            "ballpark_and_weather_simulation": {
                "wins": 0, "losses": 0, "weight": 1.0, 
                "instruction": "Evaluate Ballpark Pal venue simulations, park factors, wind vectors, temperature, air density, humidity, and roof status."
            },
            "multi_source_consensus_and_divergence": {
                "wins": 0, "losses": 0, "weight": 1.0, 
                "instruction": "Evaluate multi-model alignment across FanGraphs, Ballpark Pal, TeamRankings, Covers, and sharp money splits."
            },
            "bullpen_depth_and_fatigue": {
                "wins": 0, "losses": 0, "weight": 1.0, 
                "instruction": "Evaluate verified MLB box scores and 1-3 day rolling pitch usage only when extreme fatigue or rest disparity exists."
            },
            "umpire_and_situational_fatigue": {
                "wins": 0, "losses": 0, "weight": 1.0, 
                "instruction": "Evaluate home plate umpire strike zone tendencies, day-after-night games, cross-country travel, and getaway days."
            }
        }
    }
    with open("bot_memory.json", "w") as f:
        json.dump(default_memory, f, indent=2)
    return default_memory

def calculate_factor_weight(wins, losses):
    total = wins + losses
    if total < 3:
        return 1.0, "Baseline sample size. Standard weighting."
    win_rate = wins / total
    if win_rate >= 0.65:
        return min(1.5, round(1.0 + (win_rate - 0.5) * 1.0, 2)), f"High win rate ({round(win_rate*100, 1)}%). Prioritize this factor heavily when establishing model edge."
    elif win_rate <= 0.40:
        return max(0.3, round(1.0 - (0.5 - win_rate) * 1.2, 2)), f"Cold streak ({round(win_rate*100, 1)}%). De-emphasize as a primary driver; use only as secondary support."
    else:
        return 1.0, f"Neutral performance ({round(win_rate*100, 1)}%). Standard weighting."

def update_memory_from_sheet(sheet, memory):
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1:
            return memory

        headers = [h.strip() for h in rows[0]]
        status_idx = headers.index("Status") if "Status" in headers else 10
        pl_idx = headers.index("P/L ($)") if "P/L ($)" in headers else 11
        reason_idx = headers.index("Reasoning") if "Reasoning" in headers else 12
        high_agree_idx = 14
        for idx_h, h_name in enumerate(headers):
            if "High Agreement" in h_name:
                high_agree_idx = idx_h
                break

        wins = sum(1 for r in rows[1:] if len(r) > status_idx and str(r[status_idx]).strip().upper() == "WIN")
        losses = sum(1 for r in rows[1:] if len(r) > status_idx and str(r[status_idx]).strip().upper() == "LOSS")
        total = wins + losses

        factors = memory.get("reasoning_factor_weights", {})
        for key in factors:
            factors[key]["wins"] = 0
            factors[key]["losses"] = 0

        yes_wins, yes_losses, yes_profit = 0, 0, 0.0
        no_wins, no_losses, no_profit = 0, 0, 0.0

        keywords_map = {
            "starting_pitcher_expected_metrics": ["xfip", "siera", "xera", "fip", "csw", "whip", "k-bb", "k/bb", "starter", "strikeout", "stuff+", "location+", "pitch arsenal", "rv/100"],
            "platoon_and_lineup_splits": ["wrc+", "ops", "platoon", "vs lhp", "vs rhp", "lineup", "rest day", "handedness", "splits"],
            "statcast_contact_quality": ["statcast", "xwoba", "barrel", "hard-hit", "xba", "xslg", "babip", "savant", "exit velocity"],
            "ballpark_and_weather_simulation": ["ballpark pal", "park factor", "wind", "air density", "temperature", "humidity", "weather", "altitude", "coors", "roof"],
            "multi_source_consensus_and_divergence": ["consensus", "teamrankings", "covers", "bettingpros", "fangraphs", "ballpark pal", "model agreement", "split projection", "divergence", "sharp split", "high agreement"],
            "bullpen_depth_and_fatigue": ["bullpen", "reliever", "leverage", "closer", "3-day", "fatigue", "rosterresource", "middle relief", "high-leverage", "pitch count", "boxscore", "burned"],
            "umpire_and_situational_fatigue": ["umpire", "strike zone", "tight zone", "generous zone", "getaway day", "travel", "night-to-day", "schedule fatigue"]
        }

        for r in rows[1:]:
            if len(r) > max(status_idx, reason_idx):
                status = str(r[status_idx]).strip().upper()
                reasoning = str(r[reason_idx]).lower()
                
                try: pnl_val = float(r[pl_idx]) if len(r) > pl_idx and r[pl_idx] else 0.0
                except (ValueError, TypeError): pnl_val = 0.0

                agree_cell = str(r[high_agree_idx]).strip() if len(r) > high_agree_idx else "No"
                is_yes = agree_cell.lower().startswith("yes")

                if status in ["WIN", "LOSS"]:
                    if is_yes:
                        if status == "WIN":
                            yes_wins += 1
                            yes_profit += pnl_val
                            factors["multi_source_consensus_and_divergence"]["wins"] += 1
                        else:
                            yes_losses += 1
                            yes_profit += pnl_val
                            factors["multi_source_consensus_and_divergence"]["losses"] += 1
                    else:
                        if status == "WIN":
                            no_wins += 1
                            no_profit += pnl_val
                        else:
                            no_losses += 1
                            no_profit += pnl_val

                    for factor_key, kws in keywords_map.items():
                        if factor_key != "multi_source_consensus_and_divergence" and any(kw in reasoning for kw in kws):
                            if factor_key not in factors:
                                factors[factor_key] = {"wins": 0, "losses": 0, "weight": 1.0, "instruction": ""}
                            if status == "WIN":
                                factors[factor_key]["wins"] += 1
                            else:
                                factors[factor_key]["losses"] += 1

        for factor_key, data in factors.items():
            w_val, inst = calculate_factor_weight(data["wins"], data["losses"])
            data["weight"] = w_val
            data["instruction"] = inst

        yes_total = yes_wins + yes_losses
        if yes_total >= 3:
            yes_win_rate = round((yes_wins / yes_total) * 100, 1)
            if yes_win_rate >= 60.0:
                factors["multi_source_consensus_and_divergence"]["instruction"] = (
                    f"EMPIRICAL LESSON: High Agreement ('Yes') bets have a {yes_win_rate}% win rate (+${round(yes_profit, 2)}). "
                    f"Prioritize High Agreement plays as strong model conviction multipliers."
                )
            elif yes_win_rate <= 40.0:
                factors["multi_source_consensus_and_divergence"]["instruction"] = (
                    f"EMPIRICAL LESSON: High Agreement ('Yes') bets are hitting only {yes_win_rate}% (${round(yes_profit, 2)}). "
                    f"Consensus is proving overvalued. De-emphasize consensus and look for sharp divergence/contrarian edges."
                )

        memory["reasoning_factor_weights"] = factors
        memory["high_agreement_yes_performance"] = {
            "wins": yes_wins,
            "losses": yes_losses,
            "win_rate": f"{round((yes_wins / yes_total)*100, 1)}%" if yes_total > 0 else "0%",
            "profit": round(yes_profit, 2)
        }
        no_total = no_wins + no_losses
        memory["high_agreement_no_performance"] = {
            "wins": no_wins,
            "losses": no_losses,
            "win_rate": f"{round((no_wins / no_total)*100, 1)}%" if no_total > 0 else "0%",
            "profit": round(no_profit, 2)
        }

        if total > 0:
            win_rate = round((wins / total) * 100, 1)
            net_pl = sum(float(r[pl_idx] or 0.0) for r in rows[1:] if len(r) > pl_idx and r[pl_idx])

            memory["total_bets"] = total
            memory["wins"] = wins
            memory["losses"] = losses
            memory["win_rate"] = f"{win_rate}%"
            memory["net_profit_dollars"] = round(net_pl, 2)

            if win_rate < 50.0:
                memory["learnings_and_adjustments"] = f"Win rate is {win_rate}% (<50%). Raising EV threshold to +10% and prioritizing high-weight factors."
            else:
                memory["learnings_and_adjustments"] = f"Win rate is {win_rate}% (profitable). Maintain current quantitative multi-factor selection criteria."

        with open("bot_memory.json", "w") as f:
            json.dump(memory, f, indent=2)

    except Exception as e:
        print(f"Memory update notice: {e}")

    return memory

# --- 7. FETCH MLB ODDS & GET TODAY'S OPEN PICKS ---
def fetch_mlb_odds(odds_key):
    url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/?apiKey={odds_key}&regions=us&markets=h2h,spreads,totals&oddsFormat=american"
    print("Fetching live MLB odds...")
    resp = requests.get(url)
    if resp.status_code == 200:
        data = resp.json()
        print(f"Fetched odds for {len(data)} games.")
        return data
    else:
        print(f"Error fetching odds: {resp.status_code}")
        return []

def get_today_existing_picks(sheet, today_date_str):
    rows = sheet.get_all_values()
    if len(rows) <= 1:
        return []

    existing = []
    for row_idx, r in enumerate(rows[1:], start=2):
        if len(r) >= 11:
            r_date = str(r[0]).strip()
            r_status = str(r[10]).strip().upper()
            if r_date == today_date_str and r_status == "PENDING":
                existing.append({
                    "row_index": row_idx,
                    "date": r[0],
                    "pulled_time": r[1] if len(r) > 1 else "",
                    "game": r[2] if len(r) > 2 else "",
                    "bet_type": r[3] if len(r) > 3 else "",
                    "pick": r[4] if len(r) > 4 else "",
                    "odds": r[5] if len(r) > 5 else "",
                    "implied_prob": r[6] if len(r) > 6 else "",
                    "model_prob": r[7] if len(r) > 7 else "",
                    "expected_value": r[8] if len(r) > 8 else "",
                    "reasoning": r[12] if len(r) > 12 else "",
                    "validation": r[13] if len(r) > 13 else "",
                    "high_agreement": r[14] if len(r) > 14 else "No"
                })
    return existing

# --- 8. GENERATE PICKS VIA GEMINI WITH GROUNDED PITCHER & BULLPEN CONTEXT ---
def parse_json_from_response(response):
    """Robust extractor for JSON responses from GenAI models."""
    raw_text = ""
    if hasattr(response, "text") and response.text:
        raw_text = response.text
    elif hasattr(response, "candidates") and response.candidates:
        parts = response.candidates[0].content.parts
        raw_text = "".join([p.text for p in parts if hasattr(p, "text") and p.text])

    raw_text = raw_text.strip()
    json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
    if json_match:
        return json.loads(json_match.group(0))
    clean_text = raw_text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean_text)

def generate_picks_and_validations(odds_data, memory, open_picks, bullpen_logs, probable_pitchers):
    print("Sending MLB odds data, confirmed pitchers, multi-factor weights, and bullpen context to Gemini...")
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    prompt = f"""
    You are an adaptive quantitative MLB betting strategist executing deep multi-variable synthesis with recursive self-learning.

    === RECURSIVE MEMORY & EMPIRICAL PERFORMANCE REFLECTION ===
    {json.dumps(memory, indent=2)}

    === REASONING FACTOR WEIGHTS (DYNAMIC LESSONS FROM GRADED OUTCOMES) ===
    {json.dumps(memory.get("reasoning_factor_weights", {}), indent=2)}

    === TODAY'S CONFIRMED STARTING PITCHERS (OFFICIAL MLB STATS API) ===
    {json.dumps(probable_pitchers, indent=2)}

    === RECENT OFFICIAL MLB BULLPEN USAGE CONTEXT (LAST 48 HOURS) ===
    {json.dumps(bullpen_logs, indent=2)}

    CRITICAL REASONING & GROUND-TRUTH DIRECTIVES:
    1. GROUND STARTING PITCHER ANALYSIS IN REAL DATA:
       - Starting pitching dictates 60-70% of early-game scoring. Look at the CONFIRMED STARTING PITCHERS provided above.
       - NEVER invert starter profiles! If Pitcher A has 128 K / 31 BB in 115 IP (2.97 ERA) and Pitcher B has 61 K / 24 BB in 92 IP (3.11 ERA), Pitcher A is the superior swing-and-miss strikeout pitcher.
       - Do NOT invent fake xFIP/SIERA numbers that contradict their true strikeout and walk baselines.

    2. BALANCED REASONING FOCUS:
       - Lead with Starting Pitcher matchup dynamics (handedness, command, strikeout baseline, pitch arsenals) and Lineup Platoon splits (wRC+ vs LHP/RHP, Statcast xwOBA/Hard-Hit%).
       - Bullpen usage is a SECONDARY modifier: ONLY mention bullpen fatigue if a team suffered severe, verified arm strain (e.g. 4+ relievers thrown yesterday or 40+ pitches on back-to-back days) from the logs above. If both relief units are normal, focus strictly on starters, lineups, and venue physics.
       - Ballpark Pal physics: Incorporate park factors, wind vector/speed, temperature, and roof status.

    3. MULTI-SOURCE CONSENSUS (COLUMN 15):
       - Cross-reference daily computer projections and consensus picks from FanGraphs (ZiPS/Steamer), Ballpark Pal simulations, TeamRankings computer projections, Covers, and BettingPros sharp/public splits.
       - In "high_agreement", explicitly state the model names and simulated values (e.g. "Yes (FanGraphs: PHI 62%, BallparkPal: PHI 5.8-3.2, TeamRankings: 64%)" or "No (Split: FanGraphs on NYY 52% vs BallparkPal on BAL 54%)").

    === ACTIVE OPEN PICKS ALREADY LOGGED TODAY ===
    {json.dumps(open_picks, indent=2)}

    === TODAY'S LIVE ODDS DATA ===
    {json.dumps(odds_data[:8])}

    STRICT SPORTSBOOK CONSTRAINTS:
    - Place bets ONLY on: 1. FanDuel, 2. DraftKings, 3. BetMGM, 4. Caesars.

    STRICT RE-EVALUATION & SYNTHESIS RULES:
    1. EVALUATE EXISTING OPEN PICKS:
       For every pick listed in ACTIVE OPEN PICKS:
       - If you agree with the current side/pick: 
         Set "action": "VALIDATED". 
         Provide updated "updated_odds", "updated_model_prob", "updated_expected_value", detailed "high_agreement", and updated "reason" anchored to confirmed starter metrics and platoon splits.
         DO NOT create a duplicate entry in "new_picks".
       - If market movement, pitching change, or deeper multi-model divergence proves the OPPOSITE side is now superior:
         Set "action": "REJECTED" with "reason".
         Put the NEW opposite pick into "new_picks".

    2. NEW MATCHUPS:
       - For games that have NO existing pick in ACTIVE OPEN PICKS, select the highest +EV bet and put it in "new_picks".
       - Never recommend more than 5 total active bets across the entire slate.

    OUTPUT SCHEMA (STRICT JSON ONLY):
    {{
      "validations": [
        {{
          "row_index": <int matching row_index in open_picks>,
          "action": "VALIDATED" or "REJECTED",
          "updated_odds": <int or float, e.g. -110>,
          "updated_implied_prob": "52.4%",
          "updated_model_prob": "58.0%",
          "updated_expected_value": "+10.7%",
          "high_agreement": "<Specific source breakdown, e.g. Yes (FanGraphs: PHI 62%, BallparkPal: PHI 5.8-3.2, TeamRankings: 64%)>",
          "reason": "<balanced multi-factor reasoning citing verified starter metrics, platoon wRC+, Statcast xwOBA, venue physics, and consensus>"
        }}
      ],
      "new_picks": [
        {{
          "date": "YYYY-MM-DD",
          "game": "Away Team @ Home Team",
          "bet_type": "Moneyline (FanDuel)",
          "pick": "Team Name",
          "odds": -110,
          "implied_prob": "52.4%",
          "model_prob": "58.0%",
          "expected_value": "+10.7%",
          "high_agreement": "<Specific source breakdown, e.g. Yes (FanGraphs: PHI 62%, BallparkPal: PHI 5.8-3.2, TeamRankings: 64%)>",
          "units": 1.0,
          "reasoning": "<balanced multi-factor reasoning citing verified starter metrics, platoon wRC+, Statcast xwOBA, venue physics, and consensus>"
        }}
      ]
    }}
    """

    candidate_models = [
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.1-pro-preview"
    ]

    for model_name in candidate_models:
        for attempt in range(2):
            try:
                print(f"Attempting pick synthesis with model: {model_name}...")
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                return parse_json_from_response(response)

            except errors.ClientError as e:
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    time.sleep(5)
                elif "404" in str(e):
                    print(f"Model {model_name} returned 404, falling back to next model...")
                    break
                else:
                    print(f"Gemini API Error with {model_name}: {e}")
                    break
            except Exception as e:
                print(f"Error during pick generation with {model_name}: {e}")
                break

    return {"validations": [], "new_picks": []}

# --- 9. GAME-LEVEL DEDUPLICATION HELPER ---
def extract_canonical_teams_from_game(game_str):
    parts = re.split(r'\b(?:at|vs|v|@)\b', str(game_str), flags=re.IGNORECASE)
    cleaned = [match_canonical_team(p) for p in parts if p.strip()]
    return tuple(sorted(cleaned))

def game_already_pending(raw_rows, pick_date, game):
    """Checks if there is already an active pending pick for this exact game today using canonical team names."""
    if len(raw_rows) <= 1:
        return False

    headers = [h.strip() for h in raw_rows[0]]
    try:
        date_col = headers.index("Date")
        game_col = headers.index("Game")
        status_col = headers.index("Status")
    except ValueError:
        return False

    norm_teams = extract_canonical_teams_from_game(game)

    for r in raw_rows[1:]:
        if len(r) <= max(date_col, game_col, status_col):
            continue

        r_date = str(r[date_col]).strip()
        r_teams = extract_canonical_teams_from_game(r[game_col])
        r_status = str(r[status_col]).strip().upper()

        if r_date == pick_date and r_teams == norm_teams and r_status == "PENDING":
            return True

    return False

# --- MAIN EXECUTION ---
def main():
    spreadsheet, sheet = get_sheets()
    ensure_headers(sheet)
    ensure_evolution_sheet(spreadsheet)

    odds_key = os.environ.get("ODDS_API_KEY")
    graded_count = 0
    if odds_key:
        graded_count = auto_grade_pending_bets(sheet, odds_key)

    update_scoreboard(spreadsheet)

    memory = load_memory()
    updated_memory = update_memory_from_sheet(sheet, memory)
    today_date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    current_time_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S EDT")

    print(f"Memory Loaded | Total Bets: {updated_memory['total_bets']} | Win Rate: {updated_memory['win_rate']}")

    update_evolution_log(spreadsheet, "MLB", updated_memory, f"Execution run. Graded {graded_count} bet(s). Evaluating live board.", current_time_str)

    # 1. Fetch Confirmed Probable Pitchers directly from Official MLB API
    probable_pitchers = fetch_today_probable_pitchers(today_date_str)

    # 2. Fetch Real Bullpen Usage directly by gamePk from Official MLB API
    bullpen_logs = fetch_recent_bullpen_usage(days_back=2)

    # 3. Fetch Live Odds
    odds = fetch_mlb_odds(odds_key)
    if not odds:
        print("WARNING: No live MLB odds returned. Completed grading and evolution log.")
        return

    # 4. Process Re-Evaluations and New Picks
    open_picks = get_today_existing_picks(sheet, today_date_str)
    ai_response = generate_picks_and_validations(odds, updated_memory, open_picks, bullpen_logs, probable_pitchers)

    validations = ai_response.get("validations", [])
    new_picks = ai_response.get("new_picks", [])

    val_notes = []
    
    # 5. Process Validations & In-Place Updates on Existing Rows
    if validations:
        print(f"Processing {len(validations)} pick validation update(s)...")
        for val in validations:
            row_idx = val.get("row_index")
            action = str(val.get("action", "")).strip().upper()
            reason = str(val.get("reason", "")).strip()

            if row_idx and action in ["VALIDATED", "REJECTED"]:
                sheet.update_cell(row_idx, 14, action)
                val_notes.append(f"Row {row_idx} ({action}): {reason}")

                if action == "VALIDATED":
                    if "updated_odds" in val and val["updated_odds"]:
                        sheet.update_cell(row_idx, 6, int(round(float(val["updated_odds"]))))
                    if "updated_implied_prob" in val and val["updated_implied_prob"]:
                        sheet.update_cell(row_idx, 7, val["updated_implied_prob"])
                    if "updated_model_prob" in val and val["updated_model_prob"]:
                        sheet.update_cell(row_idx, 8, val["updated_model_prob"])
                    if "updated_expected_value" in val and val["updated_expected_value"]:
                        sheet.update_cell(row_idx, 9, val["updated_expected_value"])
                    if "high_agreement" in val and val["high_agreement"]:
                        sheet.update_cell(row_idx, 15, str(val["high_agreement"]))
                    if reason:
                        sheet.update_cell(row_idx, 13, reason)
                    sheet.update_cell(row_idx, 2, current_time_str)

                print(f"Row {row_idx} evaluated as {action}.")

    # 6. Append Only Genuinely New / Replacement Picks
    raw_rows = sheet.get_all_values()
    appended_count = 0
    skipped_count = 0

    for p in new_picks:
        pick_date = str(p.get("date", today_date_str)).strip()
        game = str(p.get("game", "")).strip()
        bet_type = str(p.get("bet_type", "")).strip()
        pick = str(p.get("pick", "")).strip()
        high_agree_detail = str(p.get("high_agreement", "No (Split consensus)"))
        
        try:
            odds_val = float(p.get("odds", -110))
        except (ValueError, TypeError):
            odds_val = -110.0

        if game_already_pending(raw_rows, pick_date, game):
            print(f"Skipping duplicate game prediction: {game} | {pick}")
            skipped_count += 1
            continue

        sheet.append_row([
            pick_date,
            current_time_str,
            game,
            bet_type,
            pick,
            int(round(odds_val)),
            p.get("implied_prob", ""),
            p.get("model_prob", ""),
            p.get("expected_value", ""),
            p.get("units", 1.0),
            "PENDING",
            0.0,
            p.get("reasoning", ""),
            "NEW",
            high_agree_detail
        ])
        appended_count += 1

    print(f"MLB Execution Complete: {appended_count} new pick(s) added, {len(validations)} validation(s) processed.")

if __name__ == "__main__":
    main()
