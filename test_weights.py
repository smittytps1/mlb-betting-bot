import os
import json
import gspread

def diagnose_weights():
    # 1. Load credentials and connect to sheet
    service_account_str = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    if not service_account_str:
        print("Error: GCP_SERVICE_ACCOUNT_JSON environment variable missing.")
        return
    
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    client = gspread.service_account_from_dict(json.loads(service_account_str), scopes=scopes)
    
    try:
        spreadsheet = client.open("MLB AI Betting Tracker")
        sheet = spreadsheet.worksheet("OG Predictor")
    except Exception as e:
        print(f"Error opening Google Sheet: {e}")
        return
    
    rows = sheet.get_all_values()
    if len(rows) <= 1:
        print("Sheet is empty or missing data.")
        return

    headers = [h.strip() for h in rows[0]]
    date_idx = headers.index("Date") if "Date" in headers else 0
    status_idx = headers.index("Status") if "Status" in headers else 10
    pl_idx = headers.index("P/L ($)") if "P/L ($)" in headers else 11
    reason_idx = headers.index("Reasoning") if "Reasoning" in headers else 12

    graded_rows = []
    for r in rows[1:]:
        if len(r) > max(status_idx, reason_idx, date_idx):
            row_date = str(r[date_idx]).strip()
            if row_date <= "2026-08-23":
                continue
            status = str(r[status_idx]).strip().upper()
            if status in ["WIN", "LOSS"]:
                graded_rows.append(r)

    graded_rows.reverse()
    print(f"Total Graded Rows (Post-Aug 23): {len(graded_rows)}\n")

    # Keywords map currently used in the predictor
    keywords_map = {
        "starting_pitcher_expected_metrics": ["xfip", "siera", "xera", "fip", "csw", "whip", "starting pitcher", "rotation advantage"],
        "platoon_and_lineup_splits": ["wrc+", "ops", "platoon split", "vs lhp", "vs rhp", "lineup advantage", "hitting split"],
        "statcast_contact_quality": ["xwoba", "barrel rate", "hard-hit rate", "xba", "xslg", "contact quality"],
        "bullpen_depth_and_fatigue": ["bullpen load", "relief backend", "closer b2b", "taxed relief", "exhausted bullpen", "relief corps"],
        "umpire_and_situational_fatigue": ["umpire zone", "getaway day", "travel fatigue", "park factor", "altitude impact", "weather conditions"]
    }

    factor_ledgers = {k: {"matches": 0, "wins": 0.0, "losses": 0.0, "net_profit": 0.0} for k in keywords_map}

    for i, r in enumerate(graded_rows):
        status = str(r[status_idx]).strip().upper()
        reasoning = str(r[reason_idx]).lower()
        try: profit_val = float(r[pl_idx]) if len(r) > pl_idx and r[pl_idx] else 0.0
        except: profit_val = 0.0

        decay_weight = 1.0
        if i >= 50:
            exponent = ((i - 50) // 5) + 1
            decay_weight = 0.95 ** exponent

        for factor_key, kws in keywords_map.items():
            if any(kw in reasoning for kw in kws):
                factor_ledgers[factor_key]["matches"] += 1
                if status == "WIN":
                    factor_ledgers[factor_key]["wins"] += decay_weight
                    factor_ledgers[factor_key]["net_profit"] += (profit_val * decay_weight)
                else:
                    factor_ledgers[factor_key]["losses"] += decay_weight
                    factor_ledgers[factor_key]["net_profit"] += (profit_val * decay_weight)

    print("=== FACTOR DIAGNOSTIC BREAKDOWN ===")
    for k, data in factor_ledgers.items():
        raw_weight = 1.0 + (data["net_profit"] / 200.0)
        bounded_weight = max(0.75, min(1.5, round(raw_weight, 2)))
        print(f"Factor: {k}")
        print(f"  Matches Found in Text: {data['matches']}")
        print(f"  Decayed Wins: {round(data['wins'], 2)} | Decayed Losses: {round(data['losses'], 2)}")
        print(f"  Calculated Net Profit Ledger: ${round(data['net_profit'], 2)}")
        print(f"  Resulting Gradient Weight: {bounded_weight}x\n")

if __name__ == "__main__":
    diagnose_weights()
