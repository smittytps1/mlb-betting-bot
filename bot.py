def format_matchups(odds_data, probable_pitchers, objective_fatigue_ratings):
    valid = []
    dropped_tbd = []
    dropped_live = []
    
    # Get the current exact time in UTC to match the API
    current_utc = datetime.now(ZoneInfo("UTC"))
    
    for game in odds_data:
        home, away = match_canonical_team(game.get("home_team", "")), match_canonical_team(game.get("away_team", ""))
        
        commence_time_str = game.get("commence_time")
        game_time_et = "Unknown Time"
        
        if commence_time_str:
            try:
                dt_utc = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00"))
                
                # STRICT TEMPORAL GUARDRAIL: If the game has already started, skip it entirely
                if dt_utc < current_utc:
                    dropped_live.append(f"{away} @ {home}")
                    continue
                    
                dt_et = dt_utc.astimezone(ZoneInfo("America/New_York"))
                game_time_et = dt_et.strftime("%Y-%m-%d %I:%M %p EDT")
            except Exception:
                pass

        h_pitcher = probable_pitchers.get(home, "TBD")
        a_pitcher = probable_pitchers.get(away, "TBD")
        
        if "TBD" in h_pitcher or "TBD" in a_pitcher: 
            dropped_tbd.append(f"{away} @ {home}")
            continue
            
        game_copy = dict(game)
        game_copy["matchup_context"] = {
            "start_time": game_time_et,
            "away": f"{away} | Starter: {a_pitcher} | Bullpen: {objective_fatigue_ratings.get(away, 'Fresh')}",
            "home": f"{home} | Starter: {h_pitcher} | Bullpen: {objective_fatigue_ratings.get(home, 'Fresh')}"
        }
        valid.append(game_copy)
        
    if dropped_tbd:
        print(f"  [Python Guardrail] Dropped {len(dropped_tbd)} games due to TBD starters.")
    if dropped_live:
        print(f"  [Temporal Guardrail] Dropped {len(dropped_live)} live/in-play games to prevent skewed odds.")
            
    return valid
