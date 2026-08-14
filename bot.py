import os
import json
import requests
import gspread
from google.oauth2.service_account import Credentials
from google import genai

# --- 1. GOOGLE SHEETS AUTHENTICATION & SETUP ---
def get_sheet():
    print("Connecting to Google Sheets...")
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
    sheet = client.open("MLB AI Betting Tracker").sheet1
    return sheet

def ensure_headers(sheet):
    """Ensures row 1 contains bold, frozen column headers."""
    try:
        existing_rows = sheet.get_all_values()
        headers = [
            "Date", "Game", "Bet Type", "Pick", "Odds", 
            "Implied Prob (%)", "Model Prob (%)", "EV (%)", "Units", 
            "Status", "P/L ($)", "Reasoning"
        ]

        if len(existing_rows) == 0:
            print("Writing column headers...")
            sheet.append_row(headers)
            try:
                sheet.format("A1:L1", {"textFormat": {"bold": True}})
                sheet.freeze(rows=1)
            except Exception as e:
                print(f"Header formatting notice: {e}")
    except Exception as e:
        print(f"Notice while checking headers: {e}")

# --- 2. BATCH AUTO-GRADING VIA SCORES API ---
def auto_grade_pending_bets(sheet, odds_key):
    """Fetches completed MLB scores and updates PENDING rows in batch to avoid 429 quota errors."""
    try:
        records = sheet.get_all_records()
        if not records:
            return

        pending_rows = [i for i, r in enumerate(records) if str(r.get("Status", "")).upper() == "PENDING"]
        if not pending_rows:
            print("No pending bets to grade.")
            return

        print(f"Checking results for {len(pending_rows)} pending bet(s)...")
        scores_url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/scores/?apiKey={odds_key}&daysFrom=3"
        resp = requests.get(scores_url)
        if resp.status_code != 200:
            print(f"Could not fetch score data. Status code: {resp.status_code}")
            return

        scores_data = resp.json()
        updates = []

        for row_idx, r in enumerate(records, start=2):
            if str(r.get("Status", "")).upper() != "PENDING":
                continue

            game_title = str(r.get("Game", ""))
            pick = str(r.get("Pick", "")).strip()
            
            # Safe float parsing
            try:
                odds = float(r.get("Odds", -110))
            except (ValueError, TypeError):
                odds = -110.0

            try:
                units = float(r.get("Units", 1.0))
            except (ValueError, TypeError):
                units = 1.0

            # Match completed games
            for match in scores_data:
                if not match.get("completed"):
                    continue

                home_team = match.get("home_team", "")
                away_team = match.get("away_team", "")
                
                if home_team in game_title or away_team in game_title:
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

                    # Stage updates for batch write (Column J: Status, Column K: P/L)
                    updates.append({
                        "range": f"J{row_idx}:K{row_idx}",
                        "values": [[status, round(profit, 2)]]
                    })
                    break  # Exit inner loop once matched to prevent duplicate updates

        if updates:
            print(f"Batch updating {len(updates)} row(s) in Google Sheets...")
            sheet.batch_update(updates)
            print("Successfully auto-graded pending bets!")

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
    print("Sending odds data and performance memory to Gemini...")
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    prompt = f"""
    You are an adaptive quantitative MLB betting expert that learns from past performance.

    === YOUR HISTORICAL MEMORY & PERFORMANCE REFLECTION ===
    {json.dumps(memory, indent=2)}

    === TODAY'S LIVE ODDS DATA ===
    {json.dumps(odds_data[:8])}

    INSTRUCTIONS:
    1. Review your historical performance and strategy guidance in your memory above.
    2. Analyze today's games, calculate implied probabilities vs model probabilities, and select exactly 5 high-EV picks.
    3. Return strictly a JSON array of 5 objects containing:
       "date", "game", "bet_type", "pick", "odds", "implied_prob", "model_prob", "expected_value", "units", "reasoning"
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
        print("WARNING: No live odds returned. Skipping pick generation.")
        return

    picks = generate_picks(odds, updated_memory)

    print(f"Generated {len(picks)} bet pick(s). Appending to Google Sheets...")

    for p in picks:
        sheet.append_row([
            p.get("date", ""),
            p.get("game", ""),
            p.get("bet_type", ""),
            p.get("pick", ""),
            p.get("odds", ""),
            p.get("implied_prob", ""),
            p.get("model_prob", ""),
            p.get("expected_value", ""),
            p.get("units", 1.0),
            "PENDING",
            0.0,
            p.get("reasoning", "")
        ])
    
    print("Successfully published adaptive picks!")

if __name__ == "__main__":
    main()
