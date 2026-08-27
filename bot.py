import os
import json
import re
import time
import math
import requests
import gspread
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from google import genai
from google.oauth2.service_account import Credentials

# --- TEAM NAME NORMALIZATION & ALIAS MAPPING ---
MLB_TEAM_ALIASES = {
    "arizona diamondbacks": ["arizona diamondbacks", "diamondbacks", "d-backs", "dbacks", "ari", "arizona"],
    "atlanta braves": ["atlanta braves", "braves", "atl", "atlanta"],
    "baltimore orioles": ["baltimore orioles", "orioles", "o's", "os", "bal", "baltimore"],
    "boston red sox": ["boston red sox", "red sox", "bos", "boston"],
    "chicago white sox": ["chicago white sox", "white sox", "cws", "chw", "chicago sox"],
    "chicago cubs": ["chicago cubs", "cubs", "chc", "cubs"],
    "cincinnati reds": ["cincinnati reds", "reds", "cin", "cincinnati"],
    "cleveland guardians": ["cleveland guardians", "guardians", "cle", "cleveland", "indians"],
    "colorado rockies": ["colorado rockies", "rockies", "col", "colorado"],
    "detroit tigers": ["detroit tigers", "tigers", "det", "detroit"],
    "houston astros": ["houston astros", "astros", "hou", "houston"],
    "kansas city royals": ["kansas city royals", "royals", "kc", "kansas city"],
    "los angeles angels": ["los angeles angels", "angels", "laa", "anaheim"],
    "los angeles dodgers": ["los angeles dodgers", "dodgers", "lad", "la dodgers"],
    "miami marlins": ["miami marlins", "marlins", "mia", "miami"],
    "milwaukee brewers": ["milwaukee brewers", "brewers", "mil", "milwaukee"],
    "minnesota twins": ["minnesota twins", "twins", "min", "minnesota"],
    "new york mets": ["new york mets", "mets", "nym", "ny mets"],
    "new york yankees": ["new york yankees", "yankees", "nyy", "ny yankees"],
    "oakland athletics": ["oakland athletics", "athletics", "a's", "as", "oak", "oakland", "sacramento athletics", "las vegas athletics"],
    "philadelphia phillies": ["philadelphia phillies", "phillies", "philly", "phi", "philadelphia"],
    "pittsburgh pirates": ["pittsburgh pirates", "pirates", "bucs", "pit", "pittsburgh"],
    "san diego padres": ["san diego padres", "padres", "sd", "san diego"],
    "san francisco giants": ["san francisco giants", "giants", "sf", "san francisco"],
    "seattle mariners": ["seattle mariners", "mariners", "sea", "seattle"],
    "st. louis cardinals": ["st. louis cardinals", "cardinals", "cards", "stl", "st louis cardinals", "st. louis", "st louis"],
    "tampa bay rays": ["tampa bay rays", "rays", "tb", "tampa bay", "tampa"],
    "texas rangers": ["texas rangers", "rangers", "tex", "texas"],
    "toronto blue jays": ["toronto blue jays", "blue jays", "jays", "tor", "toronto"],
    "washington nationals": ["washington nationals", "nationals", "nats", "wsh", "was", "washington"]
}

# MLB Ballpark Run Factor Multipliers (1.00 = Neutral)
PARK_FACTORS = {
    "Colorado Rockies": 1.34,
    "Boston Red Sox": 1.12,
    "Cincinnati Reds": 1.11,
    "Texas Rangers": 1.06,
    "Kansas City Royals": 1.05,
    "Chicago Cubs": 1.04,
    "Baltimore Orioles": 1.03,
    "Philadelphia Phillies": 1.03,
    "Washington Nationals": 1.02,
    "Atlanta Braves": 1.02,
    "Arizona Diamondbacks": 1.01,
    "Houston Astros": 1.01,
    "Milwaukee Brewers": 1.00,
    "Los Angeles Dodgers": 1.00,
    "Toronto Blue Jays": 1.00,
    "Minnesota Twins": 0.99,
    "New York Yankees": 0.99,
    "Los Angeles Angels": 0.98,
    "St. Louis Cardinals": 0.97,
    "Chicago White Sox": 0.97,
    "Cleveland Guardians": 0.96,
    "Detroit Tigers": 0.95,
    "Pittsburgh Pirates": 0.94,
    "Miami Marlins": 0.93,
    "New York Mets": 0.92,
    "Tampa Bay Rays": 0.91,
    "Oakland Athletics": 0.90,
    "San Francisco Giants": 0.86,
    "San Diego Padres": 0.86,
    "Seattle Mariners": 0.85
}

ALLOWED_SPORTSBOOKS = ["FanDuel", "DraftKings", "BetMGM", "Caesars"]

def normalize_text(text):
    return re.sub(r'[^a-z0-9]', '', str(text).lower())

def match_canonical_team(name_str):
    if not name_str: return ""
    cleaned = str(name_str).strip().lower()
    cleaned_norm = normalize_text(cleaned)
    for canonical, aliases in MLB_TEAM_ALIASES.items():
        for alias in aliases:
            if alias == cleaned or normalize_text(alias) == cleaned_norm or re.search(rf'\b{re.escape(alias)}\b', cleaned):
                return canonical.title()
    return name_str.strip().title()

def american_to_decimal(odds):
    try:
        odds_f = float(odds)
        return (odds_f / 100.0) + 1.0 if odds_f > 0 else (100.0 / abs(odds_f)) + 1.0
    except Exception:
        return 1.91

def compute_quarter_kelly_units(odds, model_prob_str):
    try:
        odds_val = float(odds)
        prob_val = float(str(model_prob_str).replace('%', '').strip()) / 100.0
        dec_odds = american_to_decimal(odds_val)
        b = dec_odds - 1.0
        if b <= 0: return 1.0
        kelly = (b * prob_val - (1.0 - prob_val)) / b
        if kelly <= 0: return 0.5
        raw_units = (kelly * 0.25) * 40.0
        
        if odds_val < 100:
            return max(0.5, min(1.15, round(raw_units, 2)))
        else:
            return max(0.5, min(1.50, round(raw_units, 2)))
    except Exception:
        return 1.0

def poisson_probability(lam, k):
    if lam <= 0: return 0.0
    return (math.exp(-lam) * (lam ** int(k))) / math.factorial(int(k))

def calculate_runline_prob(lam_fav, lam_dog):
    prob_cover = 0.0
    for f in range(2, 21):
        for d in range(0, f - 1):
            prob_cover += poisson_probability(lam_fav, f) * poisson_probability(lam_dog, d)
    return max(0.01, min(0.99, prob_cover))

def calculate_total_prob(lam_total, line, is_over=True):
    prob_exact = 0.0
    numeric_line = float(line)
    for total_runs in range(0, 30):
        p = poisson_probability(lam_total, total_runs)
        if is_over and total_runs > numeric_line: prob_exact += p
        elif not is_over and total_runs < numeric_line: prob_exact += p
    return max(0.05, min(0.95, prob_exact))

def get_sheets():
    print("Connecting to Google Sheets ('MLB' Tab)...")
    service_account_str = os.environ.get("GCP_SERVICE_ACCOUNT_JSON")
    if not service_account_str: raise ValueError("GCP_SERVICE_ACCOUNT_JSON missing!")
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(service_account_str)
    credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(credentials)
    spreadsheet = client.open("MLB AI Betting Tracker")
    return spreadsheet, spreadsheet.worksheet("MLB")

def ensure_headers(sheet):
    try:
        existing = sheet.get_all_values()
        headers = [
            "Date", "Pulled Time", "Game", "Bet Type / Sportsbook", "Pick", "Odds", 
            "Implied Prob (%)", "Model Prob (%)", "EV (%)", "Units", "Status", "P/L ($)", 
            "Reasoning", "Validation", "High Agreement & Source Breakdown", "Game Start Time", "Updated Reasoning"
        ]
        if not existing or not existing[0] or existing[0][0] != "Date" or len(existing[0]) < 17: 
            sheet.update(range_name='A1:Q1', values=[headers])
    except Exception: pass

def ensure_evolution_sheet(spreadsheet):
    try:
        try: evo_sheet = spreadsheet.worksheet("Evolution & Learnings")
        except Exception: evo_sheet = spreadsheet.add_worksheet(title="Evolution & Learnings", rows=200, cols=10)
        if not evo_sheet.get_all_values():
            evo_sheet.insert_row(["Timestamp", "Sport", "Total Bets Evaluated", "Win Rate (%)", "Net Profit ($)", "Reasoning Factor Weights", "Active Strategy Adjustment", "Validation & Re-Synthesis Notes"], index=1)
        return evo_sheet
    except Exception: return None

def update_evolution_log(spreadsheet, sport_label, memory, dynamic_learning_note, summary, time_str):
    try:
        evo_sheet = ensure_evolution_sheet(spreadsheet)
        if not evo_sheet: return
        factors = memory.get("reasoning_factor_weights", {})
        weights_str = " | ".join([f"{k}: {v.get('weight', 1.0)}x" for k, v in factors.items()]) if factors else "Standard (1.0x)"
        evo_sheet.append_row([
            time_str, sport_label, memory.get("total_bets", 0), memory.get("win_rate", "0%"), 
            memory.get("net_profit_dollars", 0.0), weights_str, 
            dynamic_learning_note, summary
        ])
    except Exception as e:
        print(f"Notice while logging to Evolution tab: {e}")

def fetch_past_evolution_learnings(spreadsheet):
    try:
        evo_sheet = spreadsheet.worksheet("Evolution & Learnings")
        rows = evo_sheet.get_all_values()
        if len(rows) <= 1: return "No prior evolution history recorded yet."
        recent_rows = rows[-10:] 
        history_summary = []
        for r in recent_rows:
            if len(r) >= 8: history_summary.append(f"Date: {r[0]} | Bets: {r[2]} | Win Rate: {r[3]} | Net P/L: ${r[4]} | Weights: {r[5]} | Notes: {r[6]}")
        return "\n".join(history_summary)
    except Exception: return "Evolution tab unavailable."

def fetch_team_advanced_metrics():
    metrics_map = {}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        teams_url = "https://statsapi.mlb.com/api/v1/teams?sportId=1"
        teams_resp = requests.get(teams_url, headers=headers, timeout=10)
        if teams_resp.status_code == 200:
            for t in teams_resp.json().get("teams", []):
                t_id = t.get("id")
                canonical = match_canonical_team(t.get("name", ""))
                if not canonical: continue
                metrics_map[canonical] = {"ops": 0.720, "iso": 0.150, "whip": 1.30, "runs_per_game": 4.5}
                stats_url = f"https://statsapi.mlb.com/api/v1/teams/{t_id}/stats?stats=season&group=hitting,pitching"
                stats_resp = requests.get(stats_url, headers=headers, timeout=5)
                if stats_resp.status_code == 200:
                    for stat_group in stats_resp.json().get("stats", []):
                        group_name = stat_group.get("group", {}).get("displayName")
                        splits = stat_group.get("splits", [])
                        if not splits: continue
                        stat_data = splits[0].get("stat", {})
                        if group_name == "hitting":
                            avg = float(stat_data.get("avg", ".240"))
                            slg = float(stat_data.get("slg", ".400"))
                            metrics_map[canonical]["ops"] = float(stat_data.get("ops", ".720"))
                            metrics_map[canonical]["iso"] = round(slg - avg, 3)
                            metrics_map[canonical]["runs_per_game"] = float(stat_data.get("runsScoredPerGame", "4.5"))
                        elif group_name == "pitching":
                            metrics_map[canonical]["whip"] = float(stat_data.get("whip", "1.30"))
    except Exception: pass
    return metrics_map

def fetch_today_probable_pitchers(target_date_str):
    pitcher_map = {}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={target_date_str}&hydrate=probablePitcher"
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            dates = resp.json().get("dates", [])
            if dates:
                for game in dates[0].get("games", []):
                    for side in ["away", "home"]:
                        team_data = game.get("teams", {}).get(side, {})
                        team_name = match_canonical_team(team_data.get("team", {}).get("name", ""))
                        pitcher_data = team_data.get("probablePitcher", {})
                        if pitcher_data and team_name:
                            pitcher_map[team_name] = {"id": pitcher_data.get("id"), "name": pitcher_data.get("fullName", "TBD")}
    except Exception: pass
    return pitcher_map

def fetch_pitcher_season_stats(pitcher_info):
    default_stats = {"whip": 1.30, "era": 4.00}
    if not isinstance(pitcher_info, dict) or not pitcher_info.get("id"): return default_stats
    try:
        pid = pitcher_info.get("id")
        stat_url = f"https://statsapi.mlb.com/api/v1/people/{pid}/stats?stats=season&group=pitching"
        stat_resp = requests.get(stat_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if stat_resp.status_code == 200:
            stats_data = stat_resp.json().get("stats", [])
            if stats_data and stats_data[0].get("splits"):
                p_stats = stats_data[0].get("splits")[0].get("stat", {})
                return {"whip": float(p_stats.get("whip", 1.30)), "era": float(p_stats.get("era", 4.00))}
    except Exception: pass
    return default_stats

def get_mlb_teams_map():
    resp = requests.get("https://statsapi.mlb.com/api/v1/teams?sportId=1", headers={"User-Agent": "Mozilla/5.0"})
    teams = {}
    if resp.status_code == 200:
        for t in resp.json().get("teams", []): teams[t["id"]] = match_canonical_team(t["name"])
    return teams

def fetch_high_leverage_relievers(teams_map):
    current_year = datetime.now(ZoneInfo("America/New_York")).year
    headers = {"User-Agent": "Mozilla/5.0"}
    leverage_weights = {}

    for team_id in teams_map.keys():
        try:
            url = f"https://statsapi.mlb.com/api/v1/stats?stats=season&group=pitching&playerPool=all&season={current_year}&teamId={team_id}&gameType=R"
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code != 200:
                continue
                
            stats_list = resp.json().get("stats", [])
            if not stats_list:
                continue

            relievers = []
            for split in stats_list[0].get("splits", []):
                pid = split.get("player", {}).get("id")
                stat = split.get("stat", {})
                games = int(stat.get("gamesPitched", 0))
                games_started = int(stat.get("gamesStarted", 0))

                # Isolate relievers from starters
                if games > 0 and (games - games_started) >= 5:
                    relievers.append({
                        "id": pid,
                        "saves": int(stat.get("saves", 0)),
                        "holds": int(stat.get("holds", 0)),
                        "games_finished": int(stat.get("gamesFinished", 0))
                    })

            if not relievers:
                continue

            # Identify Closer (Top save producer, tiebreaker games finished)
            relievers_by_saves = sorted(relievers, key=lambda x: (x["saves"], x["games_finished"]), reverse=True)
            primary_closer = relievers_by_saves[0]
            if primary_closer["saves"] >= 2 or primary_closer["games_finished"] >= 5:
                leverage_weights[primary_closer["id"]] = 2.0

            # Identify Setup Men (Next top 2 hold producers)
            remaining_relievers = [r for r in relievers if r["id"] != primary_closer["id"]]
            relievers_by_holds = sorted(remaining_relievers, key=lambda x: x["holds"], reverse=True)
            
            for setup_man in relievers_by_holds[:2]:
                if setup_man["holds"] >= 2:
                    leverage_weights[setup_man["id"]] = 1.5

        except Exception:
            continue

    return leverage_weights

def fetch_situational_fatigue_and_bullpen(days_back_bp=2, days_back_schedule=7):
    teams_map = get_mlb_teams_map()
    today = datetime.now(ZoneInfo("America/New_York")).date()
    headers = {"User-Agent": "Mozilla/5.0"}
    team_stats = {name: {"appearances": 0, "total_pitches": 0, "bp_dates": set(), "schedule_games_7d": 0, "high_lev_used": False} for name in teams_map.values()}
    
    # Map out the dynamic hierarchy of high-leverage arms
    leverage_weights = fetch_high_leverage_relievers(teams_map)

    for d in range(1, days_back_schedule + 1):
        target_date = (today - timedelta(days=d)).strftime("%Y-%m-%d")
        schedule_resp = requests.get(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={target_date}", headers=headers, timeout=10)
        if schedule_resp.status_code != 200: continue
        dates = schedule_resp.json().get("dates", [])
        if not dates: continue

        for game in dates[0].get("games", []):
            if game.get("status", {}).get("abstractGameState") != "Final": continue
            game_pk = game.get("gamePk")
            for side in ["away", "home"]:
                team_id = game.get("teams", {})[side].get("team", {}).get("id")
                canonical = teams_map.get(team_id)
                if canonical: team_stats[canonical]["schedule_games_7d"] += 1

            if d <= days_back_bp:
                box_resp = requests.get(f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore", headers=headers, timeout=10)
                if box_resp.status_code != 200: continue
                box_data = box_resp.json()
                for side in ["away", "home"]:
                    team_box = box_data.get("teams", {})[side]
                    canonical = teams_map.get(team_box.get("team", {}).get("id"))
                    if not canonical: continue
                    pitchers = team_box.get("pitchers", [])
                    players = team_box.get("players", {})
                    
                    if len(pitchers) > 1:
                        relief_pitcher_ids = pitchers[1:]
                        game_relief_pitches = 0
                        for pid in relief_pitcher_ids:
                            p_stats = players.get(f"ID{pid}", {}).get("stats", {}).get("pitching", {})
                            raw_pitches = int(p_stats.get("pitches", p_stats.get("numberOfPitches", 0)))
                            
                            # Apply dynamic tier multiplier
                            weight = leverage_weights.get(pid, 1.0)
                            game_relief_pitches += (raw_pitches * weight)
                            
                            if weight >= 1.5:
                                team_stats[canonical]["high_lev_used"] = True
                                
                        if game_relief_pitches > 0:
                            team_stats[canonical]["total_pitches"] += game_relief_pitches
                            team_stats[canonical]["bp_dates"].add(target_date)
                            team_stats[canonical]["appearances"] += len(relief_pitcher_ids)

    objective_ratings = {}
    for team, stats in team_stats.items():
        total_p = stats["total_pitches"]
        load = round(float(total_p) / float(days_back_bp), 1) if total_p > 0 else 0.0
        
        # New robust thresholds to accommodate 1.5x/2.0x multipliers
        if load >= 90.0:
            status = "TAXED"
        elif load >= 65.0:
            status = "MODERATELY WORKED"
        else:
            status = "FRESH"
            
        objective_ratings[team] = {
            "status_string": f"Status: {status} | Load Index: {load} | Relief Apps: {stats['appearances']} | Weighted Pitches (2 Days): {total_p} | Games Played (Last 7 Days): {stats['schedule_games_7d']}",
            "load": load,
            "closer_b2b": stats["high_lev_used"] and len(stats["bp_dates"]) >= 2
        }
    return objective_ratings

def load_memory():
    if os.path.exists("bot_memory.json"):
        try:
            with open("bot_memory.json", "r") as f: return json.load(f)
        except Exception: pass
    return {"total_bets": 0, "wins": 0, "losses": 0, "reasoning_factor_weights": {"starting_pitcher_expected_metrics": {"weight": 1.0}, "platoon_and_lineup_splits": {"weight": 1.0}, "bullpen_depth_and_fatigue": {"weight": 1.0}}}

def calculate_factor_weight(wins, losses):
    total = wins + losses
    if total < 3: return 1.0, "Baseline sample size."
    win_rate = wins / total
    if win_rate >= 0.55: return min(1.5, round(1.0 + (win_rate - 0.5) * 1.0, 2)), f"High win rate ({round(win_rate*100, 1)}%)."
    elif win_rate <= 0.45: return max(0.3, round(1.0 - (0.5 - win_rate) * 1.2, 2)), f"Cold streak ({round(win_rate*100, 1)}%)."
    else: return 1.0, f"Neutral performance ({round(win_rate*100, 1)}%)."

def update_memory_from_sheet(sheet, memory):
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1: return memory
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
            "starting_pitcher_expected_metrics": ["whip", "sp", "era"],
            "platoon_and_lineup_splits": ["ops", "iso", "lineup"],
            "bullpen_depth_and_fatigue": ["bullpen", "load", "taxed", "relief"]
        }

        for r in rows[1:]:
            if len(r) > max(status_idx, reason_idx):
                status = str(r[status_idx]).strip().upper()
                reasoning = str(r[reason_idx]).lower()
                if status in ["WIN", "LOSS"]:
                    for factor_key, kws in keywords_map.items():
                        if any(kw in reasoning for kw in kws):
                            if factor_key not in factors: factors[factor_key] = {"wins": 0, "losses": 0, "weight": 1.0, "instruction": ""}
                            if status == "WIN": factors[factor_key]["wins"] += 1
                            else: factors[factor_key]["losses"] += 1

        for factor_key, data in factors.items():
            w_val, inst = calculate_factor_weight(data["wins"], data["losses"])
            data["weight"] = w_val
            data["instruction"] = inst

        if total > 0:
            memory["total_bets"] = total
            memory["wins"] = wins
            memory["losses"] = losses
            memory["win_rate"] = f"{round((wins / total) * 100, 1)}%"
            memory["net_profit_dollars"] = round(sum(float(r[pl_idx] or 0.0) for r in rows[1:] if len(r) > pl_idx and r[pl_idx]), 2)
        
        with open("bot_memory.json", "w") as f: json.dump(memory, f, indent=2)
    except Exception: pass
    return memory

def calculate_strict_baseline(away, home, a_pitcher_info, h_pitcher_info, fatigue_data, advanced_metrics, memory):
    home_prob = 0.50 
    math_log = []
    weights = memory.get("reasoning_factor_weights", {})
    bp_weight = weights.get("bullpen_depth_and_fatigue", {}).get("weight", 1.0)
    ops_weight = weights.get("platoon_and_lineup_splits", {}).get("weight", 1.0)
    whip_weight = weights.get("starting_pitcher_expected_metrics", {}).get("weight", 1.0)
    
    a_sp_stats = fetch_pitcher_season_stats(a_pitcher_info)
    h_sp_stats = fetch_pitcher_season_stats(h_pitcher_info)
    a_name_str = a_pitcher_info.get("name", "TBD") if isinstance(a_pitcher_info, dict) else str(a_pitcher_info)
    h_name_str = h_pitcher_info.get("name", "TBD") if isinstance(h_pitcher_info, dict) else str(h_pitcher_info)

    sp_shift = max(-0.10, min(0.10, ((a_sp_stats["whip"] - h_sp_stats["whip"]) / 0.10) * 0.02 * whip_weight))
    home_prob += sp_shift
    math_log.append(f"SP WHIP ({a_name_str}: {a_sp_stats['whip']} vs {h_name_str}: {h_sp_stats['whip']}) | Shift: {round(sp_shift*100, 2)}%")

    a_ops = advanced_metrics.get(away, {}).get("ops", 0.720)
    h_ops = advanced_metrics.get(home, {}).get("ops", 0.720)
    ops_shift = max(-0.08, min(0.08, ((h_ops - a_ops) / 0.050) * 0.015 * ops_weight))
    home_prob += ops_shift
    math_log.append(f"OPS Shift ({a_ops} vs {h_ops}): {round(ops_shift*100, 2)}%")

    a_iso = advanced_metrics.get(away, {}).get("iso", 0.150)
    h_iso = advanced_metrics.get(home, {}).get("iso", 0.150)
    iso_shift = max(-0.05, min(0.05, ((h_iso - a_iso) / 0.020) * 0.01))
    home_prob += iso_shift
    math_log.append(f"Contact Quality ISO Shift: {round(iso_shift*100, 2)}%")

    a_load = fatigue_data.get(away, {}).get("load", 15.0)
    h_load = fatigue_data.get(home, {}).get("load", 15.0)
    bp_shift = max(-0.06, min(0.06, ((a_load - h_load) / 50.0) * 0.015 * bp_weight))
    home_prob += bp_shift
    math_log.append(f"Bullpen Load Shift: {round(bp_shift*100, 2)}%")

    a_games_7d = fatigue_data.get(away, {}).get("schedule_games_7d", 5)
    h_games_7d = fatigue_data.get(home, {}).get("schedule_games_7d", 5)
    sched_shift = max(-0.04, min(0.04, (a_games_7d - h_games_7d) * 0.005))
    home_prob += sched_shift
    math_log.append(f"Situational Schedule Shift (Games: {a_games_7d}v{h_games_7d}): {round(sched_shift*100, 2)}%")

    home_prob += 0.015
    math_log.append("HFA: +1.50%")
    home_prob = max(0.35, min(0.65, home_prob))
    
    # Park Factor Adjusted Expected Run Totals
    park_mult = PARK_FACTORS.get(home, 1.00)
    raw_total = advanced_metrics.get(away, {}).get("runs_per_game", 4.5) + advanced_metrics.get(home, {}).get("runs_per_game", 4.5)
    projected_total = round(raw_total * park_mult, 1)
    math_log.append(f"Park Factor ({home}): {park_mult}x -> Projected Total: {projected_total}")
    
    return home_prob, 1.0 - home_prob, projected_total, " | ".join(math_log)

def auto_grade_pending_bets(sheet, odds_key):
    try:
        rows = sheet.get_all_values()
        if len(rows) <= 1: return 0
        headers = [h.strip() for h in rows[0]]
        status_idx, game_idx, bet_type_idx, pick_idx, odds_idx, units_idx = headers.index("Status"), headers.index("Game"), headers.index("Bet Type / Sportsbook"), headers.index("Pick"), headers.index("Odds"), headers.index("Units")
        
        pending_rows = [(i, r) for i, r in enumerate(rows[1:], start=2) if len(r) > status_idx and str(r[status_idx]).strip().upper() == "PENDING"]
        if not pending_rows: return 0

        resp = requests.get(f"https://api.the-odds-api.com/v4/sports/baseball_mlb/scores/?apiKey={odds_key}&daysFrom=3")
        if resp.status_code != 200: return 0
        scores_data = resp.json()
        updates = []

        for row_idx, r in pending_rows:
            pick_date_str, game_title, bet_type, pick_str = str(r[0]).strip(), str(r[game_idx]).strip(), str(r[bet_type_idx]).strip().lower(), str(r[pick_idx]).strip()
            odds = float(r[odds_idx]) if r[odds_idx] else -110.0
            units = float(r[units_idx]) if r[units_idx] else 1.0

            for match in scores_data:
                if not match.get("completed"): continue
                match_date_ny_str = ""
                if match.get("commence_time"):
                    try: match_date_ny_str = datetime.fromisoformat(match.get("commence_time").replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
                    except Exception: pass
                
                if pick_date_str != match_date_ny_str: continue
                home_team, away_team = match.get("home_team", ""), match.get("away_team", "")
                if match_canonical_team(home_team) in game_title or match_canonical_team(away_team) in game_title:
                    scores = match.get("scores")
                    if not scores or len(scores) < 2: continue
                    home_score = next((int(s["score"]) for s in scores if s["name"] == home_team), 0)
                    away_score = next((int(s["score"]) for s in scores if s["name"] == away_team), 0)
                    total_score = home_score + away_score
                    
                    status = "PENDING"
                    if "moneyline" in bet_type:
                        winner = home_team if home_score > away_score else away_team
                        status = "WIN" if match_canonical_team(pick_str).lower() == match_canonical_team(winner).lower() else "LOSS"
                    elif "spread" in bet_type or "run line" in bet_type:
                        favored_team = pick_str.rsplit(' ', 1)[0]
                        spread_val = float(pick_str.split(' ')[-1]) if len(pick_str.split(' ')) > 1 else 0.0
                        h_covered = (home_score + spread_val) > away_score if match_canonical_team(favored_team).lower() == match_canonical_team(home_team).lower() else (away_score + spread_val) > home_score
                        status = "WIN" if h_covered else "LOSS"
                    elif "total" in bet_type or "over" in bet_type or "under" in bet_type:
                        line_val = float(pick_str.split(' ')[-1]) if len(pick_str.split(' ')) > 1 else 0.0
                        if "over" in pick_str.lower(): status = "WIN" if total_score > line_val else "LOSS" if total_score < line_val else "PENDING"
                        else: status = "WIN" if total_score < line_val else "LOSS" if total_score > line_val else "PENDING"

                    if status != "PENDING":
                        profit = ((odds / 100.0) * 100.0 * units) if (status == "WIN" and odds > 0) else ((100.0 / abs(odds)) * 100.0 * units) if status == "WIN" else (-100.0 * units)
                        updates.append({"range": f"K{row_idx}:L{row_idx}", "values": [[status, round(profit, 2)]]})
                    break
        
        if updates: sheet.batch_update(updates)
        return len(updates)
    except Exception: return 0

def get_today_existing_picks_detailed(sheet, today_date_str):
    rows = sheet.get_all_values()
    if len(rows) <= 1: return []
    existing = []
    for idx, r in enumerate(rows[1:], start=2):
        if len(r) > 10 and str(r[0]).strip() == today_date_str and str(r[10]).strip().upper() == "PENDING":
            existing.append({"row_index": idx, "game": str(r[2]).strip(), "bet_type": str(r[3]).strip(), "pick": str(r[4]).strip(), "odds": float(r[5]) if r[5] else -110.0})
    return existing

def fetch_mlb_odds(odds_key):
    resp = requests.get(f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/?apiKey={odds_key}&regions=us&markets=h2h,spreads,totals&oddsFormat=american")
    return resp.json() if resp.status_code == 200 else []

def format_matchups(odds_data, probable_pitchers, objective_fatigue_ratings, advanced_metrics, memory, today_date_str):
    valid, current_utc = [], datetime.now(ZoneInfo("America/New_York"))
    for game in odds_data:
        home, away = match_canonical_team(game.get("home_team", "")), match_canonical_team(game.get("away_team", ""))
        game_date_et, game_time_et = "", "Unknown Time"
        if game.get("commence_time"):
            try:
                dt_utc = datetime.fromisoformat(game.get("commence_time").replace("Z", "+00:00"))
                if dt_utc < current_utc: continue
                dt_et = dt_utc.astimezone(ZoneInfo("America/New_York"))
                game_date_et, game_time_et = dt_et.strftime("%Y-%m-%d"), dt_et.strftime("%Y-%m-%d %I:%M %p EDT")
            except Exception: pass

        if game_date_et != today_date_str: continue

        h_pitcher_info = probable_pitchers.get(home, {"name": "TBD", "id": None})
        a_pitcher_info = probable_pitchers.get(away, {"name": "TBD", "id": None})
        
        home_prob, away_prob, projected_total, math_log = calculate_strict_baseline(away, home, a_pitcher_info, h_pitcher_info, objective_fatigue_ratings, advanced_metrics, memory)
        
        home_rl_cover = calculate_runline_prob(projected_total * home_prob, projected_total * away_prob)
        away_rl_cover = calculate_runline_prob(projected_total * away_prob, projected_total * home_prob)
        
        default_bp = {"status_string": "Status: FRESH | Load Index: 0.0"}
        away_bp_str = objective_fatigue_ratings.get(away, default_bp).get('status_string', "")
        home_bp_str = objective_fatigue_ratings.get(home, default_bp).get('status_string', "")
        
        game_copy = dict(game)
        game_copy["matchup_context"] = {
            "start_time": game_time_et,
            "away": f"{away} | Starter: {a_pitcher_info.get('name')} | Bullpen: {away_bp_str} | OPS: {advanced_metrics.get(away, {}).get('ops', 0.720)} | WHIP: {advanced_metrics.get(away, {}).get('whip', 1.30)}",
            "home": f"{home} | Starter: {h_pitcher_info.get('name')} | Bullpen: {home_bp_str} | OPS: {advanced_metrics.get(home, {}).get('ops', 0.720)} | WHIP: {advanced_metrics.get(home, {}).get('whip', 1.30)}"
        }
        game_copy["python_math_baseline"] = {
            "away_win_prob_baseline": f"{round(away_prob * 100, 1)}%", "home_win_prob_baseline": f"{round(home_prob * 100, 1)}%",
            "projected_total_runs": projected_total, "calculation_log": math_log
        }
        valid.append(game_copy)
    return valid

def parse_json_from_response(response):
    raw_text = "".join([p.text for p in response.candidates[0].content.parts if hasattr(p, "text") and p.text]) if hasattr(response, "candidates") and response.candidates else getattr(response, "text", "")
    json_match = re.search(r'```(?:json)?\s*(.*?)\s*```', raw_text.strip(), re.DOTALL)
    try: return json.loads(json_match.group(1)) if json_match else json.loads(raw_text.replace("```json", "").replace("```", "").strip())
    except Exception: return {}

def generate_mlb_picks(formatted_games, open_picks, memory, past_learnings_text):
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    prompt = f"""
    You are an elite Bounded Multi-Factor Sports Betting Analyst. Python has implemented Poisson distributions, Park Factors, and a 6-metric baseline. Do NOT output any probability above 65%.

    === HISTORICAL REPORT CARD & EVOLUTION LEARNINGS ===
    {past_learnings_text}

    === TODAY'S MATCHUPS & BASELINES ===
    {json.dumps(formatted_games, indent=2)}

    === ACTIVE PENDING PICKS ===
    {json.dumps(open_picks, indent=2)}

    STRICT STRATEGY RULES:
    1. APPROVED SPORTSBOOKS ONLY: Pick ONLY from: {ALLOWED_SPORTSBOOKS}.
    2. THE LEASH (±5.0% MAX): Adjust probabilities by a maximum of ± 5.0% based on holistic contextual intuition.
    3. TIERED EV THRESHOLDS:
       - Plus-Money Underdogs (+120 or higher): >= 11.0% EV.
       - Favorites & -1.5 Run Lines: >= 7.0% EV.
       - Totals (Over/Under): >= 12.0% EV.
    4. PROHIBIT JUICED +1.5 UNDERDOGS: Do NOT pick +1.5 underdog spreads with odds worse than -125 (e.g., -135, -156). If an underdog offers value, take their straight plus-money Moneyline instead.
    5. RESPECT PARK FACTORS ON TOTALS: Python has integrated Park Factors into 'projected_total_runs'. Do NOT force Over bets in severe pitcher parks (Petco, Oracle, T-Mobile) unless both bullpens are completely broken.
    6. MAX-EV SIDE SELECTION: If multiple markets clear thresholds, ONLY output the ONE market with the HIGHEST EV.
    7. SMART VALIDATION: If pre-game odds are unavailable for pending picks, output action "VALIDATED" to keep them as PENDING.
    8. REASONING REQUIREMENT: Dive straight into the metrics and narrative without any introductory filler phrase.

    OUTPUT SCHEMA (STRICT JSON):
    {{
      "evolution_learning_note": "Write a dynamic 2-sentence meta-analysis of recent wins/losses summarizing specific metrics that are over- or under-performing (e.g., avoiding overvaluing SP WHIP against elite lineups or respecting park factors on totals).",
      "validations": [
        {{ "row_index": <int>, "action": "VALIDATED" or "REJECTED", "updated_odds": <num>, "updated_model_prob": "58.0%", "updated_expected_value": "+11.2%", "reason": "<tight summary>" }}
      ],
      "mlb_tab_picks": [
        {{ "date": "YYYY-MM-DD", "start_time": "YYYY-MM-DD HH:MM PM EDT", "game": "Away @ Home", "bet_type": "Moneyline (FanDuel)", "pick": "Team Name", "odds": 140, "implied_prob": "41.6%", "model_prob": "55.0%", "expected_value": "+13.2%", "high_agreement": "Consensus", "reasoning": "SP WHIP [away: val vs home: val], OPS Shift [val]... followed by narrative.", "ai_contextual_shift": "Shifted +X%" }}
      ]
    }}
    """
    for model_name in ["gemini-3.1-pro-preview", "gemini-3.7-flash"]:
        for _ in range(2):
            try: return parse_json_from_response(client.models.generate_content(model=model_name, contents=prompt))
            except Exception: time.sleep(5)
    return {"validations": [], "mlb_tab_picks": []}

def main():
    spreadsheet, mlb_sheet = get_sheets()
    ensure_headers(mlb_sheet)
    
    odds_key = os.environ.get("ODDS_API_KEY")
    graded_count = auto_grade_pending_bets(mlb_sheet, odds_key)
    
    memory = update_memory_from_sheet(mlb_sheet, load_memory())
    today_date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    current_time_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S EDT")
    
    past_learnings_text = fetch_past_evolution_learnings(spreadsheet)
    probable_pitchers = fetch_today_probable_pitchers(today_date_str)
    advanced_metrics = fetch_team_advanced_metrics()
    fatigue_data = fetch_situational_fatigue_and_bullpen(days_back_bp=2, days_back_schedule=7)
    
    odds = fetch_mlb_odds(odds_key)
    if not odds: return

    formatted_games = format_matchups(odds, probable_pitchers, fatigue_data, advanced_metrics, memory, today_date_str)
    if not formatted_games: return

    open_picks_detailed = get_today_existing_picks_detailed(mlb_sheet, today_date_str)
    ai_response = generate_mlb_picks(formatted_games, open_picks_detailed, memory, past_learnings_text)
    
    # Log to Evolution Tab with dynamic AI learning note
    learning_note = ai_response.get("evolution_learning_note", "Maintain balanced quantitative multi-factor evaluation.")
    update_evolution_log(spreadsheet, "MLB", memory, learning_note, f"Execution run. Graded {graded_count} bets.", current_time_str)

    validations = ai_response.get("validations", [])
    if validations:
        active_game_names = [match_canonical_team(g.get("home_team", "")) for g in odds] + [match_canonical_team(g.get("away_team", "")) for g in odds]
        for val in validations:
            row_idx = val.get("row_index")
            action = str(val.get("action", "")).strip().upper()
            try:
                if row_idx:
                    row_vals = mlb_sheet.row_values(row_idx)
                    if len(row_vals) > 10 and str(row_vals[10]).upper() == "PENDING" and not any(t in row_vals[2] for t in active_game_names): continue
                    mlb_sheet.update_cell(row_idx, 14, action)
                    if action == "VALIDATED":
                        if val.get("updated_odds"): mlb_sheet.update_cell(row_idx, 6, int(round(float(val.get("updated_odds")))))
                        if val.get("updated_model_prob"): mlb_sheet.update_cell(row_idx, 8, val.get("updated_model_prob"))
                        if val.get("updated_expected_value"): mlb_sheet.update_cell(row_idx, 9, val.get("updated_expected_value"))
                    elif action == "REJECTED":
                        mlb_sheet.update_cell(row_idx, 11, "REJECTED")
                        mlb_sheet.update_cell(row_idx, 12, 0.0)
                    if val.get("reason"): mlb_sheet.update_cell(row_idx, 17, str(val.get("reason")).strip())
                    mlb_sheet.update_cell(row_idx, 2, current_time_str)
                    time.sleep(0.5)
            except Exception: pass

    mlb_picks = ai_response.get("mlb_tab_picks", [])
    existing_signatures = [f"{r[2]} | {r[3]}" for r in mlb_sheet.get_all_values()[1:]] if len(mlb_sheet.get_all_values()) > 1 else []
    
    for p in mlb_picks:
        game, bet_type_label = str(p.get("game", "")).strip(), str(p.get("bet_type", "")).strip()
        if not any(sb.lower() in bet_type_label.lower() for sb in ALLOWED_SPORTSBOOKS) or f"{game} | {bet_type_label}" in existing_signatures: continue
        model_prob_str = str(p.get("model_prob", "50.0%"))
        try: odds_val = float(p.get("odds", -110))
        except: odds_val = -110.0
        mlb_sheet.append_row([
            str(p.get("date", today_date_str)).strip(), current_time_str, game, bet_type_label, str(p.get("pick", "")).strip(), int(round(odds_val)),
            p.get("implied_prob", ""), model_prob_str, p.get("expected_value", ""), compute_quarter_kelly_units(odds_val, model_prob_str), "PENDING", 0.0, 
            str(p.get("reasoning", "")).strip(), "NEW", p.get("high_agreement", "No"), str(p.get("start_time", "")).strip(), ""
        ], value_input_option="USER_ENTERED")
        existing_signatures.append(f"{game} | {bet_type_label}")
        time.sleep(0.5)

if __name__ == "__main__":
    main()
