import os
import json
import requests
import gspread
from datetime import datetime
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials
from google import genai

# --- 1. GOOGLE SHEETS AUTHENTICATION & SETUP ---
def get_sheet():
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
    return sheet

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

# --- 2. BATCH AUTO-GRADING VIA SCORES API ---
def auto_grade_pending_bets(sheet, odds_key):
    """Fetches completed MLB scores and updates PENDING rows safely in batch."""
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

            game_date = str(r.get("Date", "")).strip()
            game_title = str(r.get("Game", ""))
            pick = str(r.get("Pick", "")).strip()
            
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
                commence_time = match.get("commence_time", "")[:10]

                if (home_team in game_title or away_team in game_title) and (commence_time >= game_date):
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

                    print(f"Graded Row {row_idx}: {game_title} ({commence_time}) -> {status} (${round(profit, 2)})")

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

# --- 3. MEMORY MANAGEMENT & ANALYTICS ---
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

# --- 4. FETCH MLB ODDS ---
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

# --- 5. GENERATE PICKS VIA GEMINI ---
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
    sheet = get_sheet()
    ensure_headers(sheet)
    odds_key = os.environ.get("ODDS_API_KEY")

    if odds_key:
        auto_grade_pending_bets(sheet, odds_key)

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
        game = p.get("game", "")
        bet_type = p.get("bet_type", "")
        pick = p.get("pick", "")
        
        try:
            odds_val = float(p.get("odds", -110))
        except (ValueError, TypeError):
            odds_val = -110.0

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
            print(f"Skipping duplicate pick: {game} | {pick} @ {odds_val}")
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
