import os
import json
import requests
import gspread
from google.oauth2.service_account import Credentials
import google.generativeai as genai

# --- 1. GOOGLE SHEETS AUTHENTICATION ---
def get_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    service_account_str = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    if not service_account_str:
        raise ValueError("GCP_SERVICE_ACCOUNT_JSON env var missing.")
    
    creds_dict = json.loads(service_account_str)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open("MLB AI Betting Tracker").sheet1

# --- 2. FETCH MLB ODDS ---
def fetch_mlb_odds():
    odds_key = os.environ.get("ODDS_API_KEY")
    url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/?apiKey={odds_key}®ions=us&markets=h2h,spreads,totals&oddsFormat=american"
    resp = requests.get(url)
    if resp.status_code == 200:
        return resp.json()
    return []

# --- 3. FETCH MLB SCORES FOR EVALUATION ---
def fetch_mlb_scores():
    odds_key = os.environ.get("ODDS_API_KEY")
    url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/scores/?apiKey={odds_key}&daysFrom=1"
    resp = requests.get(url)
    return resp.json() if resp.status_code == 200 else []

# --- 4. MEMORY MANAGEMENT ---
def load_memory():
    if os.path.exists("bot_memory.json"):
        with open("bot_memory.json", "r") as f:
            return json.load(f)
    return {"win_rate": "N/A", "notes": "No historical bet evaluations yet."}

def save_memory(memory_data):
    with open("bot_memory.json", "w") as f:
        json.dump(memory_data, f, indent=2)

# --- 5. GENERATE PICKS VIA GEMINI ---
def generate_picks(odds_data, memory):
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel("gemini-2.5-flash")

    prompt = f"""
    You are a quantitative MLB betting expert.
    Historical Memory/Performance Insights: {json.dumps(memory)}
    
    Today's Live Odds Data:
    {json.dumps(odds_data[:8])}

    Analyze the games, calculate implied vs model probabilities, and select exactly 5 high-EV bets.
    Return strictly a JSON array of 5 objects containing:
    "date", "game", "bet_type", "pick", "odds", "implied_prob", "model_prob", "expected_value", "units", "reasoning"
    """

    response = model.generate_content(prompt)
    clean_json = response.text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean_json)

# --- MAIN RUN LOGIC ---
def main():
    sheet = get_sheet()
    
    # Ensure headers exist
    if len(sheet.get_all_values()) == 0:
        headers = ["Date", "Game", "Bet Type", "Pick", "Odds", "Implied Prob", "Model Prob", "EV", "Units", "Status", "P/L", "Reasoning"]
        sheet.append_row(headers)

    memory = load_memory()
    odds = fetch_mlb_odds()

    if odds:
        picks = generate_picks(odds, memory)
        for p in picks:
            sheet.append_row([
                p.get("date", ""), p.get("game", ""), p.get("bet_type", ""),
                p.get("pick", ""), p.get("odds", ""), p.get("implied_prob", ""),
                p.get("model_prob", ""), p.get("expected_value", ""), p.get("units", 1.0),
                "PENDING", 0.0, p.get("reasoning", "")
            ])
        print("Successfully published 5 bets to Google Sheets!")

if __name__ == "__main__":
    main()