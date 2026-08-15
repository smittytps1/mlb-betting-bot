import os
import json
import re
import time
import requests
import gspread
from datetime import datetime
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials
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
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    
    spreadsheet = client.open("MLB AI Betting Tracker")
    sheet = spreadsheet.worksheet("MLB")
    return spreadsheet, sheet

def ensure_headers(sheet):
    """Ensures row 1 contains bold, frozen column headers including Validation."""
    try:
        existing_rows = sheet.get_all_values()
        headers = [
            "Date", "Pulled Time", "Game", "Bet Type / Sportsbook", "Pick", "Odds", 
            "Implied Prob (%)", "Model Prob (%)", "EV (%)", "Units", 
            "Status", "P/L ($)", "Reasoning", "Validation"
        ]

        if len(existing_rows) == 0 or (len(existing_rows) > 0 and existing_rows[0][0] != "Date"):
            print("Writing MLB column headers to row 1...")
            sheet.insert_row(headers, index=1)
            try:
                sheet.format("A1:N1", {"textFormat": {"bold": True}})
                sheet.freeze(rows=1)
            except Exception as e:
                print(f"Header formatting notice: {e}")
        else:
            if len(existing_rows[0]) < 14 or existing_rows[0][13] != "Validation":
                sheet.update_cell(1, 14, "Validation")
                sheet.format("N1", {"textFormat": {"bold": True}})
    except Exception as e:
        print(f"Notice while checking headers: {e}")

# --- 2. ACCURATE AUTO-GRADING VIA SCORES API ---
def auto_grade_pending_bets(sheet, odds_key):
    """Grades PENDING bets strictly if the game's start time is AFTER the prediction's pulled time."""
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1:
            return

        headers = [h.strip() for h in rows[0]]
        
        try:
            status_idx = headers.index("Status")
            game_idx = headers.index("Game")
            pick_idx = headers.index("Pick")
            pulled_idx = headers.index("Pulled Time")
            odds_idx = headers.index("Odds")
            units_idx = headers.index("Units")
        except ValueError as e:
            print(f"Auto-grading skipped: Missing required header column - {e}")
            return

        pending_rows = []
        for row_idx, r in enumerate(rows[1:], start=2):
            if len(r) > status_idx and str(r[status_idx]).strip().upper() == "PENDING":
                pending_rows.append((row_idx, r))

        if not pending_rows:
            print("No pending MLB bets to grade.")
            return

        print(f"Checking results for {len(pending_rows)} pending MLB bet(s)...")
        scores_url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/scores/?apiKey={odds_key}&daysFrom=3"
        resp = requests.get(scores_url)
        if resp.status_code != 200:
            print(f"Could not fetch MLB score data. Status code: {resp.status_code}")
            return

        scores_data = resp.json()
        updates = []

        for row_idx, r in pending_rows:
            game_title = str(r[game_idx]).strip()
            pick = str(r[pick_idx]).strip()
            pulled_time_raw = str(r[pulled_idx]).strip()
            
            try:
                odds = float(r[odds_idx])
            except (ValueError, TypeError):
                odds = -110.0

            try:
                units = float(r[units_idx]) if len(r) > units_idx and r[units_idx] else 1.0
            except (ValueError, TypeError):
                units = 1.0

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

                    winner = home_team if home_score > away_score else away_team
                    is_win = (pick in winner or winner in pick)
                    status = "WIN" if is_win else "LOSS"

                    if is_win:
                        profit = (100 / abs(odds)) * 100 * units if odds < 0 else (odds / 100) * 100 * units
                    else:
                        profit = -100.0 * units

                    print(f"Graded Row {row_idx}: {game_title} -> {status} (${round(profit, 2)})")

                    updates.append({
                        "range": f"K{row_idx}:L{row_idx}",
                        "values": [[status, round(profit, 2)]]
                    })
                    break

        if updates:
            print(f"Batch updating {len(updates)} row(s) in MLB tab...")
            sheet.batch_update(updates)
            print("Successfully auto-graded pending MLB bets!")

    except Exception as e:
        print(f"Auto-grading completed with notice: {e}")

# --- 3. SCOREBOARD UPDATER ---
def update_scoreboard(spreadsheet):
    """Ensures the 'Scoreboard' tab exists and contains dynamic formulas for both bots."""
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
        sb_sheet.format("A1:F1", {"textFormat": {"bold": True}})
        sb_sheet.format("E2:E4", {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}})
        sb_sheet.format("F2:F4", {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"}})

        print("Scoreboard tab successfully updated with live formulas!")
    except Exception as e:
        print(f"Notice while updating Scoreboard: {e}")

# --- 4. MEMORY MANAGEMENT & REASONING FACTOR WEIGHTING ---
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
        "learnings_and_adjustments": "Maintain balanced quantitative evaluation.",
        "reasoning_factor_weights": {
            "bullpen_and_rest": {
                "wins": 0,
                "losses": 0,
                "weight": 1.0,
                "instruction": "Standard weighting on bullpen fatigue and leverage usage."
            },
            "contact_vs_strikeout": {
                "wins": 0,
                "losses": 0,
                "weight": 1.0,
                "instruction": "Standard weighting on contact-rate matchups vs high-strikeout pitchers."
            },
            "pitcher_underlying_metrics": {
                "wins": 0,
                "losses": 0,
                "weight": 1.0,
                "instruction": "Standard weighting on WHIP, K/BB ratios, and xFIP."
            },
            "weather_and_wind": {
                "wins": 0,
                "losses": 0,
                "weight": 1.0,
                "instruction": "Standard weighting on stadium weather and wind conditions."
            }
        }
    }
    with open("bot_memory.json", "w") as f:
        json.dump(default_memory, f, indent=2)
    return default_memory

def calculate_factor_weight(wins, losses):
    total = wins + losses
    if total < 3:
        return 1.0, "Baseline sample size. Maintain standard weighting."
    win_rate = wins / total
    if win_rate >= 0.65:
        return min(1.5, round(1.0 + (win_rate - 0.5) * 1.0, 2)), f"High win rate ({round(win_rate*100, 1)}%). Prioritize this factor heavily when establishing edge."
    elif win_rate <= 0.40:
        return max(0.3, round(1.0 - (0.5 - win_rate) * 1.2, 2)), f"Cold streak ({round(win_rate*100, 1)}%). De-emphasize as a primary driver; use only as secondary support."
    else:
        return 1.0, f"Neutral performance ({round(win_rate*100, 1)}%). Maintain standard weighting."

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

        # Track reasoning factor outcomes across settled bets
        factors = memory.get("reasoning_factor_weights", {})
        for key in factors:
            factors[key]["wins"] = 0
            factors[key]["losses"] = 0

        keywords_map = {
            "bullpen_and_rest": ["bullpen", "rest", "leverage", "fatigue", "reliever"],
            "contact_vs_strikeout": ["contact", "strikeout", "swing-and-miss", "k-rate", "whiff"],
            "pitcher_underlying_metrics": ["whip", "k/bb", "era", "fip", "underlying", "starter"],
            "weather_and_wind": ["wind", "weather", "humidity", "temperature", "park factor"]
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

        # Re-calculate dynamic weights
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
                memory["learnings_and_adjustments"] = f"Win rate is {win_rate}% (<50%). Increase EV threshold and de-emphasize low-weight factors."
            else:
                memory["learnings_and_adjustments"] = f"Win rate is {win_rate}% (profitable). Maintain current quantitative selection criteria."

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

# --- 6. GENERATE PICKS VIA GEMINI WITH WEIGHTED REASONING SYNTHESIS ---
def generate_picks_and_validations(odds_data, memory, open_picks):
    print("Sending MLB odds data, factor weights, active picks, and memory to Gemini...")
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    prompt = f"""
    You are an adaptive quantitative MLB betting expert that performs deep multi-factor synthesis.

    === YOUR HISTORICAL MEMORY & PERFORMANCE REFLECTION ===
    {json.dumps(memory, indent=2)}

    === REASONING FACTOR WEIGHTS (DYNAMIC LESSONS FROM PAST RESULTS) ===
    {json.dumps(memory.get("reasoning_factor_weights", {}), indent=2)}

    FACTOR WEIGHTING INSTRUCTIONS:
    - HIGH WEIGHT FACTORS (Weight > 1.2): Prioritize these analytical factors when calculating model probability, as they have consistently correlated with winning predictions.
    - LOW WEIGHT FACTORS (Weight < 0.8): Do NOT discard these factors completely, but DE-EMPHASIZE their influence. They MUST NOT be the primary driver or sole justification for a pick, but can be used as secondary supporting context alongside stronger factors.

    === ACTIVE OPEN PICKS PREVIOUSLY LOGGED TODAY ===
    {json.dumps(open_picks, indent=2)}

    === TODAY'S LIVE ODDS DATA ===
    {json.dumps(odds_data[:8])}

    STRICT SPORTSBOOK CONSTRAINTS:
    - Recommend bets where the pick and odds are placed on one of these 4 approved sportsbooks:
      1. FanDuel, 2. DraftKings, 3. BetMGM, 4. Caesars

    SYNTHESIS & VALIDATION INSTRUCTIONS:
    1. REVIEW PREVIOUS PICKS: Look at the active open picks previously logged today above.
    2. SYNTHESIZE OPPOSING LOGIC: If your analysis finds a compelling case for the opposite side of an existing pick, combine the arguments for BOTH teams (applying factor weights) and determine which side holds superior true +EV value.
    3. VALIDATE OR REJECT EXISTING PICKS:
       - If an existing pick remains the best stance, output an object in "validations" with action "VALIDATED".
       - If synthesized analysis shows the opposing side or a new bet is superior, mark the old pick in "validations" as action "REJECTED", and output the new superior bet in "new_picks".
    4. NEW BETS: Select up to 5 total high-EV picks for games not yet covered.

    FORMATTING REQUIREMENTS:
    Return strictly a single JSON object with two arrays: "validations" and "new_picks".

    JSON Structure:
    {{
      "validations": [
        {{
          "row_index": <int from open_picks>,
          "action": "VALIDATED" or "REJECTED",
          "reason": "<brief justification for validation or rejection>"
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
          "reasoning": "<synthesized reasoning citing weighted factors>"
        }}
      ]
    }}
    """

    max_retries = 3
    for attempt in range(max_retries):
        try:
            chat = client.chats.create(model="gemini-3.6-flash")
            response = chat.send_message(prompt)
            
            text = response.text.strip()
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if json_match:
                clean_json = json_match.group(0)
            else:
                clean_json = text.replace("```json", "").replace("```", "").strip()

            return json.loads(clean_json)

        except errors.ClientError as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                wait_time = (attempt + 1) * 10
                print(f"Rate limit (429) hit. Retrying in {wait_time} seconds (Attempt {attempt + 1}/{max_retries})...")
                time.sleep(wait_time)
            else:
                print(f"Gemini API Error: {e}")
                break
        except Exception as e:
            print(f"Unexpected error during pick generation: {e}")
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
    odds_key = os.environ.get("ODDS_API_KEY")

    if odds_key:
        auto_grade_pending_bets(sheet, odds_key)

    update_scoreboard(spreadsheet)

    memory = load_memory()
    updated_memory = update_memory_from_sheet(sheet, memory)
    today_date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    current_time_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S EDT")

    print(f"Memory Loaded | Total Bets: {updated_memory['total_bets']} | Win Rate: {updated_memory['win_rate']}")

    odds = fetch_mlb_odds(odds_key)

    if not odds:
        print("WARNING: No live MLB odds returned. Skipping pick generation.")
        return

    open_picks = get_today_existing_picks(sheet, today_date_str)
    ai_response = generate_picks_and_validations(odds, updated_memory, open_picks)

    validations = ai_response.get("validations", [])
    new_picks = ai_response.get("new_picks", [])

    if validations:
        print(f"Processing {len(validations)} pick validation update(s)...")
        for val in validations:
            row_idx = val.get("row_index")
            action = str(val.get("action", "")).strip().upper()
            if row_idx and action in ["VALIDATED", "REJECTED"]:
                sheet.update_cell(row_idx, 14, action)
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

    print(f"MLB Execution Complete: {appended_count} new/updated pick(s) added, {skipped_count} duplicate(s) skipped.")

if __name__ == "__main__":
    main()
