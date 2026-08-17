import os
import json
import re
import time
import requests
import gspread
from datetime import datetime
from zoneinfo import ZoneInfo
from google import genai
from google.genai import errors

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
    """Ensures row 1 contains column headers including Validation."""
    try:
        existing_rows = sheet.get_all_values()
        headers = [
            "Date", "Pulled Time", "Game", "Bet Type / Sportsbook", "Pick", "Odds", 
            "Implied Prob (%)", "Model Prob (%)", "EV (%)", "Units", 
            "Status", "P/L ($)", "Reasoning", "Validation"
        ]

        if not existing_rows or not existing_rows[0] or existing_rows[0][0] != "Date":
            print("Writing MLB column headers to row 1...")
            sheet.insert_row(headers, index=1)
        else:
            if len(existing_rows[0]) < 14 or existing_rows[0][13] != "Validation":
                sheet.update_cell(1, 14, "Validation")
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
    """Logs a live snapshot of the bot's learning, factor weights, and strategy evolution."""
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

# --- 2. ACCURATE AUTO-GRADING VIA SCORES API (MONEYLINES, SPREADS, TOTALS) ---
def auto_grade_pending_bets(sheet, odds_key):
    """Grades PENDING bets (Moneylines, Spreads, and Over/Under Totals)."""
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

                if home_team in game_title or away_team in game_title:
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

                    if "total" in bet_type or "over" in pick_str.lower() or "under" in pick_str.lower():
                        num_match = re.search(r'[-+]?\d*\.?\d+', pick_str)
                        if num_match:
                            total_line = float(num_match.group(0))
                            is_over = "over" in bet_type or "over" in pick_str.lower()
                            if total_score == total_line:
                                status = "PUSH"
                                profit = 0.0
                            elif (is_over and total_score > total_line) or (not is_over and total_score < total_line):
                                status = "WIN"
                            else:
                                status = "LOSS"

                    elif "spread" in bet_type or "run line" in bet_type:
                        num_match = re.search(r'[-+]?\d*\.?\d+', pick_str)
                        spread_val = float(num_match.group(0)) if num_match else 0.0
                        is_home_pick = home_team.lower() in pick_str.lower()
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

                    else:
                        winner = home_team if home_score > away_score else away_team
                        is_win = (pick_str.lower() in winner.lower() or winner.lower() in pick_str.lower())
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

# --- 3. SCOREBOARD UPDATER ---
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

# --- 4. RECURSIVE MEMORY & FACTOR WEIGHTING ---
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
        "learnings_and_adjustments": "Maintain balanced quantitative multi-factor evaluation.",
        "reasoning_factor_weights": {
            "starting_pitcher_metrics": {"wins": 0, "losses": 0, "weight": 1.0, "instruction": "Evaluate xFIP, SIERA, CSW%, K-BB%, and handedness splits."},
            "pitch_mix_arsenal_matchup": {"wins": 0, "losses": 0, "weight": 1.0, "instruction": "Evaluate pitch arsenal RV/100 vs lineup tracking."},
            "bullpen_usage_and_leverage": {"wins": 0, "losses": 0, "weight": 1.0, "instruction": "Evaluate 3-day pitch usage, leverage tier FIP, and middle relief."},
            "statcast_offensive_splits": {"wins": 0, "losses": 0, "weight": 1.0, "instruction": "Evaluate wRC+/wOBA vs pitcher hand, Barrel%, Hard-Hit%, and chase rates."},
            "ballpark_and_weather_physics": {"wins": 0, "losses": 0, "weight": 1.0, "instruction": "Evaluate park factors, wind vector, temperature, and air density."},
            "defense_umpire_and_rest": {"wins": 0, "losses": 0, "weight": 1.0, "instruction": "Evaluate DRS/OAA, catcher framing, umpire zones, and travel/rest."}
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

        wins = sum(1 for r in rows[1:] if len(r) > status_idx and str(r[status_idx]).strip().upper() == "WIN")
        losses = sum(1 for r in rows[1:] if len(r) > status_idx and str(r[status_idx]).strip().upper() == "LOSS")
        total = wins + losses

        factors = memory.get("reasoning_factor_weights", {})
        for key in factors:
            factors[key]["wins"] = 0
            factors[key]["losses"] = 0

        keywords_map = {
            "starting_pitcher_metrics": ["xfip", "siera", "csw", "whip", "k/bb", "starter", "strikeout", "handedness"],
            "pitch_mix_arsenal_matchup": ["arsenal", "pitch mix", "slider", "fastball", "changeup", "curveball", "run value", "rv/100"],
            "bullpen_usage_and_leverage": ["bullpen", "reliever", "leverage", "closer", "3-day", "fatigue", "rest advantage", "middle relief"],
            "statcast_offensive_splits": ["wrc+", "woba", "barrel", "hard-hit", "xwoba", "chase", "plate discipline", "platoon", "lineup"],
            "ballpark_and_weather_physics": ["park factor", "wind", "air density", "humidity", "temperature", "roof", "fly ball"],
            "defense_umpire_and_rest": ["drs", "oaa", "catcher framing", "umpire", "strike zone", "travel", "night-to-day", "rest"]
        }

        for r in rows[1:]:
            if len(r) > max(status_idx, reason_idx):
                status = str(r[status_idx]).strip().upper()
                reasoning = str(r[reason_idx]).lower()
                if status in ["WIN", "LOSS"]:
                    for factor_key, kws in keywords_map.items():
                        if any(kw in reasoning for kw in kws):
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

        memory["reasoning_factor_weights"] = factors

        if total > 0:
            win_rate = round((wins / total) * 100, 1)
            net_pl = sum(float(r[pl_idx] or 0.0) for r in rows[1:] if len(r) > pl_idx and r[pl_idx])

            memory["total_bets"] = total
            memory["wins"] = wins
            memory["losses"] = losses
            memory["win_rate"] = f"{win_rate}%"
            memory["net_profit_dollars"] = round(net_pl, 2)

            if win_rate < 50.0:
                memory["learnings_and_adjustments"] = f"Win rate is {win_rate}% (<50%). Raising EV threshold to +10% and de-emphasizing low-weight factors."
            else:
                memory["learnings_and_adjustments"] = f"Win rate is {win_rate}% (profitable). Maintain current quantitative multi-factor selection criteria."

        with open("bot_memory.json", "w") as f:
            json.dump(memory, f, indent=2)

    except Exception as e:
        print(f"Memory update notice: {e}")

    return memory

# --- 5. FETCH MLB ODDS & ACTIVE TODAY PICKS ---
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
                    "reasoning": r[12] if len(r) > 12 else "",
                    "validation": r[13] if len(r) > 13 else ""
                })
    return existing

# --- 6. GENERATE PICKS VIA GEMINI ---
def generate_picks_and_validations(odds_data, memory, open_picks):
    print("Sending MLB odds data, multi-factor weights, open picks, and memory to Gemini...")
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    prompt = f"""
    You are an adaptive quantitative MLB betting strategist executing deep multi-variable synthesis with recursive self-learning.

    === RECURSIVE MEMORY & PERFORMANCE REFLECTION ===
    {json.dumps(memory, indent=2)}

    === REASONING FACTOR WEIGHTS (DYNAMIC LESSONS FROM GRADED OUTCOMES) ===
    {json.dumps(memory.get("reasoning_factor_weights", {}), indent=2)}

    WEIGHTING DIRECTIVES:
    - High-Weight Factors (>1.2x): Prioritize as primary drivers of true model win probability.
    - Low-Weight Factors (<0.8x): Do NOT discard, but DE-EMPHASIZE. They must not be the sole or primary justification for an EV edge.

    === MULTI-FACTOR BASEBALL SYNTHESIS CRITERIA ===
    1. STARTING PITCHING: Predictive metrics (xFIP, SIERA, CSW%, K-BB%) over surface ERA; handedness splits; pitch arsenal vs lineup RV/100; velocity deviations.
    2. BULLPEN USAGE & DEPTH: 3-day rolling pitch counts; leverage-tier FIP vs middle-relief vulnerabilities.
    3. OFFENSIVE STATCAST SPLITS: wRC+/wOBA vs pitcher hand; Barrel%, Hard-Hit%, and chase rates.
    4. BALLPARK & WEATHER PHYSICS: Venue park factors; wind vector; temperature, humidity, air density; roof status.
    5. DEFENSE, UMPIRE & SITUATIONAL SPOTS: DRS/OAA, catcher framing, umpire strike zone tendencies, rest/travel.

    === ACTIVE OPEN PICKS PREVIOUSLY LOGGED TODAY ===
    {json.dumps(open_picks, indent=2)}

    === TODAY'S LIVE ODDS DATA ===
    {json.dumps(odds_data[:8])}

    STRICT SPORTSBOOK CONSTRAINTS:
    - Place bets ONLY on: 1. FanDuel, 2. DraftKings, 3. BetMGM, 4. Caesars.

    RE-SYNTHESIS & VALIDATION INSTRUCTIONS:
    - If you find compelling evidence opposing an active open pick, combine arguments for BOTH sides to establish one true stance.
    - If the previous pick remains best, output "action": "VALIDATED".
    - If opposing synthesis proves superior, mark the old pick as "action": "REJECTED" and output the new superior bet in "new_picks".
    - Generate up to 5 total high-EV picks for unrepresented matchups.

    OUTPUT SCHEMA:
    Return strictly a valid JSON object:
    {{
      "validations": [
        {{
          "row_index": <int>,
          "action": "VALIDATED" or "REJECTED",
          "reason": "<multi-factor synthesis justification>"
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
          "units": 1.0,
          "reasoning": "<synthesized breakdown citing starter xFIP/SIERA, bullpen rest, Statcast splits, and environment>"
        }}
      ]
    }}
    """

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            
            text = response.text.strip()
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            clean_json = json_match.group(0) if json_match else text.replace("```json", "").replace("```", "").strip()
            return json.loads(clean_json)

        except errors.ClientError as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                wait_time = (attempt + 1) * 10
                print(f"Rate limit (429) hit. Retrying in {wait_time}s (Attempt {attempt + 1}/{max_retries})...")
                time.sleep(wait_time)
            else:
                print(f"Gemini API Error: {e}")
                break
        except Exception as e:
            print(f"Error during pick generation: {e}")
            break

    return {"validations": [], "new_picks": []}

# --- 7. DEDUPLICATION HELPER ---
def normalize_string(text):
    return re.sub(r'[^a-z0-9]', '', str(text).lower())

def extract_sorted_teams(game_str):
    parts = re.split(r'\b(?:at|vs|v|@)\b', str(game_str), flags=re.IGNORECASE)
    cleaned = [normalize_string(p) for p in parts if p.strip()]
    return tuple(sorted(cleaned))

def is_duplicate_pick(raw_rows, pick_date, game, bet_type, pick, odds_val):
    if len(raw_rows) <= 1:
        return False

    headers = [h.strip() for h in raw_rows[0]]
    try:
        date_col = headers.index("Date")
        game_col = headers.index("Game")
        bet_type_col = headers.index("Bet Type / Sportsbook")
        pick_col = headers.index("Pick")
        odds_col = headers.index("Odds")
    except ValueError:
        return False

    norm_teams = extract_sorted_teams(game)
    norm_bet_type = normalize_string(bet_type)
    norm_pick = normalize_string(pick)
    try:
        norm_odds = int(round(float(odds_val)))
    except (ValueError, TypeError):
        norm_odds = 0

    for r in raw_rows[1:]:
        if len(r) <= max(date_col, game_col, bet_type_col, pick_col, odds_col):
            continue

        r_date = str(r[date_col]).strip()
        r_teams = extract_sorted_teams(r[game_col])
        r_bet_type = normalize_string(r[bet_type_col])
        r_pick = normalize_string(r[pick_col])
        
        try:
            r_odds = int(round(float(r[odds_col])))
        except (ValueError, TypeError):
            r_odds = 0

        if (r_date == pick_date and 
            r_teams == norm_teams and 
            r_bet_type == norm_bet_type and 
            r_pick == norm_pick and 
            r_odds == norm_odds):
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

    odds = fetch_mlb_odds(odds_key)
    if not odds:
        print("WARNING: No live MLB odds returned. Completed grading and evolution log.")
        return

    open_picks = get_today_existing_picks(sheet, today_date_str)
    ai_response = generate_picks_and_validations(odds, updated_memory, open_picks)

    validations = ai_response.get("validations", [])
    new_picks = ai_response.get("new_picks", [])

    val_notes = []
    if validations:
        print(f"Processing {len(validations)} pick validation update(s)...")
        for val in validations:
            row_idx = val.get("row_index")
            action = str(val.get("action", "")).strip().upper()
            reason = str(val.get("reason", "")).strip()
            if row_idx and action in ["VALIDATED", "REJECTED"]:
                sheet.update_cell(row_idx, 14, action)
                val_notes.append(f"Row {row_idx} ({action}): {reason}")
                print(f"Row {row_idx} marked as {action}.")

    raw_rows = sheet.get_all_values()
    appended_count = 0
    skipped_count = 0

    for p in new_picks:
        pick_date = str(p.get("date", today_date_str)).strip()
        game = str(p.get("game", "")).strip()
        bet_type = str(p.get("bet_type", "")).strip()
        pick = str(p.get("pick", "")).strip()
        
        try:
            odds_val = float(p.get("odds", -110))
        except (ValueError, TypeError):
            odds_val = -110.0

        if is_duplicate_pick(raw_rows, pick_date, game, bet_type, pick, odds_val):
            print(f"Skipping duplicate prediction: {game} | {pick} @ {int(round(odds_val))}")
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
            ""
        ])
        appended_count += 1

    print(f"MLB Execution Complete: {appended_count} new pick(s) added, {len(validations)} validation(s) processed.")

if __name__ == "__main__":
    main()
