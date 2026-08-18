import os
import json
import re
import time
import requests
import gspread
from datetime import datetime
from zoneinfo import ZoneInfo
from google import genai
from google.genai import types
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
    """Ensures row 1 contains all 15 column headers including High Agreement and Validation."""
    try:
        existing_rows = sheet.get_all_values()
        headers = [
            "Date", "Pulled Time", "Game", "Bet Type / Sportsbook", "Pick", "Odds", 
            "Implied Prob (%)", "Model Prob (%)", "EV (%)", "Units", 
            "Status", "P/L ($)", "Reasoning", "Validation", "High Agreement (Yes/No)"
        ]

        if not existing_rows or not existing_rows[0] or existing_rows[0][0] != "Date":
            print("Writing MLB column headers to row 1...")
            sheet.insert_row(headers, index=1)
        else:
            current_row_len = len(existing_rows[0])
            if current_row_len < 14 or existing_rows[0][13] != "Validation":
                sheet.update_cell(1, 14, "Validation")
            if current_row_len < 15 or (current_row_len >= 15 and existing_rows[0][14] != "High Agreement (Yes/No)"):
                sheet.update_cell(1, 15, "High Agreement (Yes/No)")
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

# --- 2. ACCURATE AUTO-GRADING VIA SCORES API ---
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
                        
                        is_home_pick = home_team.lower() in pick_lower
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
                        is_win = (pick_lower in winner.lower() or winner.lower() in pick_lower)
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

# --- 4. RECURSIVE MEMORY & FACTOR WEIGHTING (INCLUDING COLUMN 15 AUDITING) ---
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
            "bullpen_depth_and_fatigue": {
                "wins": 0, "losses": 0, "weight": 1.0, 
                "instruction": "Evaluate 1-3 day rolling pitch usage via RosterResource, leverage-tier SIERA, and middle relief vulnerabilities."
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
            "umpire_and_situational_fatigue": {
                "wins": 0, "losses": 0, "weight": 1.0, 
                "instruction": "Evaluate home plate umpire strike zone tendencies, day-after-night games, cross-country travel, and getaway days."
            },
            "multi_source_consensus_and_divergence": {
                "wins": 0, "losses": 0, "weight": 1.0, 
                "instruction": "Evaluate multi-model alignment across FanGraphs, Ballpark Pal, TeamRankings, Covers, and sharp money splits."
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
        high_agree_idx = headers.index("High Agreement (Yes/No)") if "High Agreement (Yes/No)" in headers else 14

        wins = sum(1 for r in rows[1:] if len(r) > status_idx and str(r[status_idx]).strip().upper() == "WIN")
        losses = sum(1 for r in rows[1:] if len(r) > status_idx and str(r[status_idx]).strip().upper() == "LOSS")
        total = wins + losses

        # 1. Reset standard factor counters
        factors = memory.get("reasoning_factor_weights", {})
        for key in factors:
            factors[key]["wins"] = 0
            factors[key]["losses"] = 0

        # 2. Track Column 15 High Agreement metrics directly
        yes_wins, yes_losses, yes_profit = 0, 0, 0.0
        no_wins, no_losses, no_profit = 0, 0, 0.0

        keywords_map = {
            "starting_pitcher_expected_metrics": ["xfip", "siera", "xera", "fip", "csw", "whip", "k-bb", "k/bb", "starter", "strikeout", "stuff+", "location+", "pitch arsenal", "rv/100"],
            "bullpen_depth_and_fatigue": ["bullpen", "reliever", "leverage", "closer", "3-day", "fatigue", "rosterresource", "middle relief", "high-leverage"],
            "platoon_and_lineup_splits": ["wrc+", "ops", "platoon", "vs lhp", "vs rhp", "lineup", "rest day", "handedness", "splits"],
            "statcast_contact_quality": ["statcast", "xwoba", "barrel", "hard-hit", "xba", "xslg", "babip", "savant", "exit velocity"],
            "ballpark_and_weather_simulation": ["ballpark pal", "park factor", "wind", "air density", "temperature", "humidity", "weather", "altitude", "coors", "roof"],
            "umpire_and_situational_fatigue": ["umpire", "strike zone", "tight zone", "generous zone", "getaway day", "travel", "night-to-day", "schedule fatigue"],
            "multi_source_consensus_and_divergence": ["consensus", "teamrankings", "covers", "bettingpros", "fangraphs projection", "model agreement", "split projection", "divergence", "sharp split", "high agreement"]
        }

        for r in rows[1:]:
            if len(r) > max(status_idx, reason_idx):
                status = str(r[status_idx]).strip().upper()
                reasoning = str(r[reason_idx]).lower()
                
                try: pnl_val = float(r[pl_idx]) if len(r) > pl_idx and r[pl_idx] else 0.0
                except (ValueError, TypeError): pnl_val = 0.0

                agree_val = str(r[high_agree_idx]).strip().capitalize() if len(r) > high_agree_idx else "No"

                if status in ["WIN", "LOSS"]:
                    # Audit Column 15 directly
                    if agree_val == "Yes":
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

                    # Audit reasoning keywords for other factors
                    for factor_key, kws in keywords_map.items():
                        if factor_key != "multi_source_consensus_and_divergence" and any(kw in reasoning for kw in kws):
                            if factor_key not in factors:
                                factors[factor_key] = {"wins": 0, "losses": 0, "weight": 1.0, "instruction": ""}
                            if status == "WIN":
                                factors[factor_key]["wins"] += 1
                            else:
                                factors[factor_key]["losses"] += 1

        # 3. Calculate dynamic factor weights
        for factor_key, data in factors.items():
            w_val, inst = calculate_factor_weight(data["wins"], data["losses"])
            data["weight"] = w_val
            data["instruction"] = inst

        # Column 15 specific directive synthesis
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

# --- 5. FETCH MLB ODDS & GET TODAY'S OPEN PICKS ---
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

# --- 6. GENERATE PICKS VIA GEMINI 3 GROUNDED WITH MULTI-SOURCE SYNTHESIS ---
def generate_picks_and_validations(odds_data, memory, open_picks):
    print("Sending MLB odds data, multi-factor weights, open picks, and analytical frameworks to Gemini...")
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    prompt = f"""
    You are an adaptive quantitative MLB betting strategist executing deep multi-variable synthesis with recursive self-learning.
    Use Google Search grounding to verify today's confirmed lineups, starting pitcher announcements, bullpen availability, stadium weather, and projection consensus across major predictive models.

    === RECURSIVE MEMORY & EMPIRICAL PERFORMANCE REFLECTION ===
    {json.dumps(memory, indent=2)}

    === REASONING FACTOR WEIGHTS (DYNAMIC LESSONS FROM GRADED OUTCOMES) ===
    {json.dumps(memory.get("reasoning_factor_weights", {}), indent=2)}

    === COLUMN 15 HIGH AGREEMENT AUDIT RESULTS ===
    - High Agreement ('Yes') Historical Record: {json.dumps(memory.get("high_agreement_yes_performance", {}))}
    - Low/Split Agreement ('No') Historical Record: {json.dumps(memory.get("high_agreement_no_performance", {}))}

    WEIGHTING DIRECTIVES:
    - High-Weight Factors (>1.2x): Prioritize as primary drivers of true model win probability.
    - Low-Weight Factors (<0.8x): Do NOT discard, but DE-EMPHASIZE. They must not be the sole or primary justification for an EV edge.

    === QUANTITATIVE RESEARCH METHODOLOGY & ANALYTICAL SOURCES ===
    1. FANGRAPHS / ADVANCED PITCHING METRICS:
       - Starting pitching dictates 60-70% of early-game scoring. Look past surface ERA.
       - Focus heavily on: FIP, xFIP, SIERA, xERA, K-BB% (best baseline command indicator), CSW%, Stuff+/Location+, and pitch mix arsenal Run Values (RV/100).
    2. ROSTERRESOURCE / BULLPEN FATIGUE & LEVERAGE:
       - Starters rarely go past 5-6 innings. Track 1-3 day rolling pitch usage and bullpen-taxing extra-inning games.
       - A taxed bullpen forced to use low-leverage arms often drives late-game scoring spikes.
    3. PLATOON & LINEUP CHANGES:
       - Check handedness splits (wRC+ and OPS vs LHP/RHP).
       - Account for confirmed daily starting 9 changes and key rest spots.
    4. BASEBALL SAVANT / STATCAST CONTACT QUALITY:
       - Evaluate xwOBA, Hard-Hit%, Barrel%, xBA, and xSLG to identify lucky BABIP anomalies vs genuine authority.
    5. BALLPARK PAL / ENVIRONMENTAL PHYSICS:
       - Incorporate stadium-specific park factors, wind vectors/speed (blowing out to center vs blowing in), temperature (warmer air is less dense = ball travels further), humidity, barometric pressure, altitude (e.g., Coors Field), and roof status.
    6. UMPIRE & SITUATIONAL SPOTS:
       - Home plate umpire strike zone tendencies: Generous zones depress scoring; tight zones inflate walks/pitch counts (favoring Overs).
       - Situational fatigue: Day games immediately following night games, getaway days, and time zone travel.
    7. MULTI-SOURCE CONSENSUS & DIVERGENCE SYNTHESIS:
       - Cross-reference daily computer projections and consensus picks from FanGraphs (ZiPS/Steamer), Ballpark Pal simulations, TeamRankings computer projections, Covers, and BettingPros sharp/public splits.
       - HIGH AGREEMENT ("Yes"): Unanimous or strong agreement across computer simulation models and sharp money on this side.
       - SPLIT/CONFLICTED ("No"): Models diverge or disagree on the projected winner/total.

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
         Provide any updated "updated_odds", "updated_model_prob", "updated_expected_value", "high_agreement": "Yes" or "No", and "reason".
         DO NOT create a duplicate entry in "new_picks".
       - If market movement, pitching change, weather shifts, or deeper multi-model divergence proves the OPPOSITE side is now superior:
         Set "action": "REJECTED" with "reason".
         Put the NEW opposite pick into "new_picks".

    2. NEW MATCHUPS:
       - For games that have NO existing pick in ACTIVE OPEN PICKS, select the highest +EV bet and put it in "new_picks".
       - Indicate "high_agreement": "Yes" if predictive models unanimously agree on this side, or "No" if mixed/split.
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
          "high_agreement": "Yes" or "No",
          "reason": "<multi-factor reasoning citing starter xFIP/SIERA, bullpen fatigue, Statcast splits, environment, and consensus alignment>"
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
          "high_agreement": "Yes" or "No",
          "units": 1.0,
          "reasoning": "<synthesized breakdown citing starter xFIP/SIERA, bullpen rest, Statcast splits, environment, and consensus alignment>"
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

    search_config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())]
    )

    for model_name in candidate_models:
        for attempt in range(2):
            try:
                print(f"Attempting pick synthesis with model: {model_name} (with Google Search Grounding)...")
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=search_config
                )
                
                text = response.text.strip()
                json_match = re.search(r'\{.*\}', text, re.DOTALL)
                clean_json = json_match.group(0) if json_match else text.replace("```json", "").replace("```", "").strip()
                return json.loads(clean_json)

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

# --- 7. GAME-LEVEL DEDUPLICATION HELPER ---
def normalize_string(text):
    return re.sub(r'[^a-z0-9]', '', str(text).lower())

def extract_sorted_teams(game_str):
    parts = re.split(r'\b(?:at|vs|v|@)\b', str(game_str), flags=re.IGNORECASE)
    cleaned = [normalize_string(p) for p in parts if p.strip()]
    return tuple(sorted(cleaned))

def game_already_pending(raw_rows, pick_date, game):
    """Checks if there is already an active pending pick for this exact game today."""
    if len(raw_rows) <= 1:
        return False

    headers = [h.strip() for h in raw_rows[0]]
    try:
        date_col = headers.index("Date")
        game_col = headers.index("Game")
        status_col = headers.index("Status")
    except ValueError:
        return False

    norm_teams = extract_sorted_teams(game)

    for r in raw_rows[1:]:
        if len(r) <= max(date_col, game_col, status_col):
            continue

        r_date = str(r[date_col]).strip()
        r_teams = extract_sorted_teams(r[game_col])
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

    odds = fetch_mlb_odds(odds_key)
    if not odds:
        print("WARNING: No live MLB odds returned. Completed grading and evolution log.")
        return

    open_picks = get_today_existing_picks(sheet, today_date_str)
    ai_response = generate_picks_and_validations(odds, updated_memory, open_picks)

    validations = ai_response.get("validations", [])
    new_picks = ai_response.get("new_picks", [])

    val_notes = []
    
    # 1. Process Validations & In-Place Updates on Existing Rows
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
                        sheet.update_cell(row_idx, 15, str(val["high_agreement"]).capitalize())
                    if reason:
                        sheet.update_cell(row_idx, 13, reason)
                    sheet.update_cell(row_idx, 2, current_time_str)

                print(f"Row {row_idx} evaluated as {action}.")

    # 2. Append Only Genuinely New / Replacement Picks
    raw_rows = sheet.get_all_values()
    appended_count = 0
    skipped_count = 0

    for p in new_picks:
        pick_date = str(p.get("date", today_date_str)).strip()
        game = str(p.get("game", "")).strip()
        bet_type = str(p.get("bet_type", "")).strip()
        pick = str(p.get("pick", "")).strip()
        high_agree = str(p.get("high_agreement", "No")).strip().capitalize()
        
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
            high_agree
        ])
        appended_count += 1

    print(f"MLB Execution Complete: {appended_count} new pick(s) added, {len(validations)} validation(s) processed.")

if __name__ == "__main__":
    main()
