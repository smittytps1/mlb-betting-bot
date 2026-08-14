import os
import json
import requests
import gspread
from google.oauth2.service_account import Credentials
from google import genai

# --- 1. GOOGLE SHEETS AUTHENTICATION & HEADERS ---
def get_sheet():
    print("Connecting to Google Sheets...")
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
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

# --- 2. SELF-LEARNING MEMORY SYSTEM ---
def load_memory():
    if os.path.exists("bot_memory.json"):
        with open("bot_memory.json", "r") as f:
            return json.load(f)
    return {
        "total_bets": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": "0%",
        "net_profit_dollars": 0.0,
        "learnings_and_adjustments": "No historical data evaluated yet. Maintain balanced evaluation."
    }

def update_memory_from_sheet(sheet, memory):
    """Calculates real-time win/loss stats from Google Sheet and saves to memory."""
    records = sheet.get_all_records()
    if not records:
        return memory

    wins = sum(1 for r in records if str(r.get("Status", "")).upper() == "WIN")
    losses = sum(1 for r in records if str(r.get("Status", "")).upper() == "LOSS")
    total = wins + losses

    if total > 0:
        win_rate = round((wins / total) * 100, 1)
        net_pl = sum(float(r.get("P/L ($)", 0.0) or 0.0) for r in records)
        
        memory["total_bets"] = total
        memory["wins"] = wins
        memory["losses"] = losses
        memory["win_rate"] = f"{win_rate}%"
        memory["net_profit_dollars"] = round(net_pl, 2)

        # Dynamic strategy guidance based on win rate
        if win_rate < 50.0:
            memory["learnings_and_adjustments"] = (
                f"Current win rate is {win_rate}% (under 50%). Tighten EV thresholds, "
                "prioritize moneyline value, reduce risk on high-volatility totals, and favor higher edge margins."
            )
        else:
            memory["learnings_and_adjustments"] = (
                f"Current win rate is {win_rate}% (profitable). Maintain current quantitative selection criteria."
            )

    # Save updated performance memory back to JSON
    with open("bot_memory.json", "w") as f:
        json.dump(memory, f, indent=2)

    return memory

# --- 3. FETCH MLB ODDS ---
def fetch_mlb_odds():
    odds_key = os.environ.get("ODDS_API_KEY")
    if not odds_key:
        print("ERROR: ODDS_API_KEY environment variable is missing!")
        return []
        
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

# --- 4. GENERATE PICKS WITH REFLECTIVE PROMPT ---
def generate_picks(odds_data, memory):
    print("Sending odds data and performance memory to Gemini...")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is missing!")

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

    # 1. Update memory based on completed bets in Google Sheets
    memory = load_memory()
    updated_memory = update_memory_from_sheet(sheet, memory)
    print(f"Memory Loaded | Total Bets: {updated_memory['total_bets']} | Win Rate: {updated_memory['win_rate']}")

    # 2. Fetch today's odds
    odds = fetch_mlb_odds()

    if not odds:
        print("WARNING: No live odds returned. Using fallback test row...")
        picks = [{
            "date": "2026-08-14",
            "game": "Test Game @ Demo Ballpark",
            "bet_type": "Moneyline",
            "pick": "Home Team",
            "odds": -110,
            "implied_prob": 52.38,
            "model_prob": 58.00,
            "expected_value": 10.70,
            "units": 1.0,
            "reasoning": "Pipeline self-learning verification"
        }]
    else:
        picks = generate_picks(odds, updated_memory)

    print(f"Generated {len(picks)} bet pick(s). Appending to Google Sheets...")

    # 3. Append picks to Google Sheet
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
