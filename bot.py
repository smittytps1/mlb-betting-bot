import os
import json
import requests
import gspread
from google.oauth2.service_account import Credentials
from google import genai

# --- 1. GOOGLE SHEETS AUTHENTICATION ---
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
    print(f"Connected successfully to Google Sheet tab: '{sheet.title}'")
    return sheet

# --- 2. FETCH MLB ODDS ---
def fetch_mlb_odds():
    odds_key = os.environ.get("ODDS_API_KEY")
    if not odds_key:
        print("ERROR: ODDS_API_KEY environment variable is missing!")
        return []
        
    url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/?apiKey={odds_key}&regions=us&markets=h2h,spreads,totals&oddsFormat=american"
    print("Fetching live MLB odds from The Odds API...")
    resp = requests.get(url)
    if resp.status_code == 200:
        data = resp.json()
        print(f"Successfully fetched odds for {len(data)} games.")
        return data
    else:
        print(f"Error fetching odds. Status Code: {resp.status_code}, Response: {resp.text}")
        return []

# --- 3. MEMORY MANAGEMENT ---
def load_memory():
    if os.path.exists("bot_memory.json"):
        with open("bot_memory.json", "r") as f:
            return json.load(f)
    return {"win_rate": "N/A", "notes": "No historical bet evaluations yet."}

# --- 4. GENERATE PICKS VIA GEMINI ---
def generate_picks(odds_data, memory):
    print("Sending odds data to Gemini for analysis...")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is missing!")

    # Initialize modern Google GenAI Client
    client = genai.Client(api_key=api_key)

    prompt = f"""
    You are a quantitative MLB betting expert.
    Historical Memory/Performance Insights: {json.dumps(memory)}
    
    Today's Live Odds Data:
    {json.dumps(odds_data[:8])}

    Analyze the games, calculate implied vs model probabilities, and select exactly 5 high-EV bets.
    Return strictly a JSON array of 5 objects containing:
    "date", "game", "bet_type", "pick", "odds", "implied_prob", "model_prob", "expected_value", "units", "reasoning"
    """

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )

    clean_json = response.text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean_json)

# --- MAIN RUN LOGIC ---
def main():
    sheet = get_sheet()
    
    # Ensure headers exist
    existing_rows = sheet.get_all_values()
    if len(existing_rows) == 0:
        print("Sheet is empty. Writing column headers...")
        headers = [
            "Date", "Game", "Bet Type", "Pick", "Odds", 
            "Implied Prob", "Model Prob", "EV", "Units", 
            "Status", "P/L", "Reasoning"
        ]
        sheet.append_row(headers)

    memory = load_memory()
    odds = fetch_mlb_odds()

    if not odds:
        print("WARNING: No odds were returned from The Odds API. Using fallback test row...")
        picks = [{
            "date": "2026-08-14",
            "game": "Test Game @ Demo Venue",
            "bet_type": "Moneyline",
            "pick": "Home Team",
            "odds": -110,
            "implied_prob": 52.38,
            "model_prob": 58.00,
            "expected_value": 10.7,
            "units": 1.0,
            "reasoning": "Test run verification"
        }]
    else:
        picks = generate_picks(odds, memory)

    print(f"Generated {len(picks)} bet pick(s). Appending rows to Google Sheets...")

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
    
    print("Successfully published bets to Google Sheets!")

if __name__ == "__main__":
    main()
