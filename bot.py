import os
import json
import requests
import gspread
from datetime import datetime
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials
from google import genai

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
    """Ensures row 1 contains bold, frozen column headers in the MLB tab."""
    try:
        existing_rows = sheet.get_all_values()
        headers = [
            "Date", "Pulled Time", "Game", "Bet Type / Sportsbook", "Pick", "Odds", 
            "Implied Prob (%)", "Model Prob (%)", "EV (%)", "Units", 
            "Status", "P/L ($)", "Reasoning"
        ]

        if len(existing_rows) == 0 or (len(existing_rows) > 0 and existing_rows[0][0] != "Date"):
            print("Writing MLB column headers to row 1...")
            sheet.insert_row(headers, index=1)
            try:
                sheet.format("A1:M1", {"textFormat": {"bold": True}})
                sheet.freeze(rows=1)
            except Exception as e:
                print(f"Header formatting notice: {e}")
        else:
            print("Headers already exist on the MLB tab.")
    except Exception as e:
        print(f"Notice while checking headers: {e}")

# --- 2. ACCURATE AUTO-GRADING VIA SCORES API ---
def auto_grade_pending_bets(sheet, odds_key):
    """Fetches completed MLB scores and grades pending rows only if game started after prediction was pulled."""
    try:
        records = sheet.get_all_records()
        if not records:
            return

        pending_rows = [i for i, r in enumerate(records) if str(r.get("Status", "")).upper() == "PENDING"]
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

        for row_idx, r in enumerate(records, start=2):
            if str(r.get("Status", "")).upper() != "PENDING":
                continue

            game_title = str(r.get("Game", ""))
            pick = str(r.get("Pick", "")).strip()
            pulled_time_str = str(r.get("Pulled Time", "")).strip()
            
            try:
                odds = float(r.get("Odds", -110))
            except (ValueError, TypeError):
                odds = -110.0

            try:
                units = float(r.get("Units", 1.0))
            except (ValueError, TypeError):
                units = 1.0

            for match in scores_data:
                if not match.get("completed"):
                    continue

                home_team = match.get("home_team", "")
                away_team = match.get("away_team", "")
                commence_time_raw = match.get("commence_time", "")  # e.g., "2026-08-15T18:10:00Z"

                # Check if game teams match
                if home_team in game_title or away_team in game_title:
                    # Validate that the completed game started AFTER or ON the day prediction was made
                    game_commence_date = commence_time_raw[:10]
                    pulled_date = pulled_time_str[:10] if len(pulled_time_str) >= 10 else str(r.get("Date", "")).strip()

                    if game_commence_date < pulled_date:
                        continue  # Skip old historical games between same teams

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

                    print(f"Graded Row {row_idx}: {game_title} ({game_commence_date}) -> {status} (${round(profit, 2)})")

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

# --- 4. MEMORY MANAGEMENT & ANALYTICS ---
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
        "learnings_and_adjustments": "No historical data evaluated yet. Maintain balanced evaluation."
    }
    with open("bot_memory.json", "w") as f:
        json.dump(default_memory, f, indent=2)
    return default_memory

def update_memory_from_sheet(sheet, memory):
    try:
        records = sheet.get_all_records()
        if not records:
            return memory

        wins = sum(1 for r in records if str(r.get("Status", "")).upper() == "WIN")
        losses = sum(1 for r in records if str(r.get("Status", "")).upper() == "LOSS")
        total = wins + losses

        if total > 0:
            win_rate = round((wins / total) * 100, 1)
            
            net_pl = 0.0
            for r in records:
                try:
                    net_pl += float(r.get("P/L ($)", 0.0) or 0.0)
                except (ValueError, TypeError):
                    pass

            memory["total_bets"] = total
            memory["wins"] = wins
            memory["losses"] = losses
            memory["win_rate"] = f"{win_rate}%"
            memory["net_profit_dollars"] = round(net_pl, 2)

            if win_rate < 50.0:
                memory["learnings_and_adjustments"] = (
                    f"Current win rate is {win_rate}% (under 50%). Increase EV threshold, "
                    "prioritize high-value moneyline picks, and avoid low-edge totals."
                )
            else:
                memory["learnings_and_adjustments"] = (
                    f"Current win rate is {win_rate}% (profitable). Maintain current quantitative selection criteria."
                )

        with open("bot_memory.json", "w") as f:
            json.dump(memory, f, indent=2)

    except Exception as e:
        print(f"Memory update notice: {e}")

    return memory

# --- 5. FETCH MLB ODDS ---
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

# --- 6. GENERATE PICKS VIA GEMINI ---
def generate_picks(odds_data, memory):
    print("Sending MLB odds data and performance memory to Gemini...")
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    prompt = f"""
    You are an adaptive quantitative MLB betting expert that learns from past performance.

    === YOUR HISTORICAL MEMORY & PERFORMANCE REFLECTION ===
    {json.dumps(memory, indent=2)}

    === TODAY'S LIVE ODDS DATA ===
    {json.dumps(odds_data[:8])}

    STRICT SPORTSBOOK CONSTRAINTS:
    - You may analyze and compare odds across ALL sportsbooks to detect market line movements.
    - However, you MUST ONLY recommend bets where the pick and odds are placed on one of these 4 approved sportsbooks:
      1. FanDuel
      2. DraftKings
      3. BetMGM
      4. Caesars

    INSTRUCTIONS:
    1. Review your historical performance and strategy guidance in your memory above.
    2. Analyze today's games, calculate implied probabilities vs model probabilities, and select exactly 5 high-EV bets.
    3. Return strictly a JSON array of 5 objects containing:
       "date", "game", "bet_type", "pick", "odds", "implied_prob", "model_prob", "expected_value", "units", "reasoning"
       
       Note for "bet_type": Format as "Moneyline (FanDuel)", "Spread (DraftKings)", "Total Over (BetMGM)", or "Moneyline (Caesars)".
    """

    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=prompt
    )

    clean_json = response.text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean_json)

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
    print(f"Memory Loaded | Total Bets: {updated_memory['total_bets']} | Win Rate: {updated_memory['win_rate']}")

    odds = fetch_mlb_odds(odds_key)

    if not odds:
        print("WARNING: No live MLB odds returned. Skipping pick generation.")
        return

    picks = generate_picks(odds, updated_memory)
    current_time_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S EDT")
    today_date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    existing_records = sheet.get_all_records()
    appended_count = 0
    skipped_count = 0

    for p in picks:
        pick_date = p.get("date", today_date_str)
        game = str(p.get("game", "")).strip()
        bet_type = str(p.get("bet_type", "")).strip()
        pick = str(p.get("pick", "")).strip()
        
        try:
            odds_val = float(p.get("odds", -110))
        except (ValueError, TypeError):
            odds_val = -110.0

        # Strict Deduplication Check: Same Game + Bet Type + Pick + Odds
        is_duplicate = False
        for r in existing_records:
            r_date = str(r.get("Date", "")).strip()
            r_game = str(r.get("Game", "")).strip()
            r_bet_type = str(r.get("Bet Type / Sportsbook", "")).strip()
            r_pick = str(r.get("Pick", "")).strip()
            
            try:
                r_odds = float(r.get("Odds", 0))
            except (ValueError, TypeError):
                r_odds = 0.0

            if (r_date == pick_date and r_game == game and r_bet_type == bet_type and r_pick == pick and r_odds == odds_val):
                is_duplicate = True
                break

        if is_duplicate:
            print(f"Skipping duplicate prediction: {game} | {pick} @ {odds_val}")
            skipped_count += 1
            continue

        sheet.append_row([
            pick_date,
            current_time_str,
            game,
            bet_type,
            pick,
            odds_val,
            p.get("implied_prob", ""),
            p.get("model_prob", ""),
            p.get("expected_value", ""),
            p.get("units", 1.0),
            "PENDING",
            0.0,
            p.get("reasoning", "")
        ])
        appended_count += 1

    print(f"MLB Execution Complete: {appended_count} new/updated pick(s) added, {skipped_count} duplicate(s) skipped.")

if __name__ == "__main__":
    main()
