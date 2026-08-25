# Process Validations with In-Progress Game Protection (Preserving Original Reasoning)
    validations = ai_response.get("validations", [])
    if validations:
        print(f"Processing {len(validations)} pick validation(s)...")
        active_game_names = [match_canonical_team(g.get("home_team", "")) for g in odds] + [match_canonical_team(g.get("away_team", "")) for g in odds]
        
        for val in validations:
            row_idx = val.get("row_index")
            action = str(val.get("action", "")).strip().upper()
            reason = str(val.get("reason", "")).strip()
            
            try:
                if row_idx:
                    row_vals = mlb_sheet.row_values(row_idx)
                    game_title = row_vals[2] if len(row_vals) > 2 else ""
                    
                    is_still_pregame = any(team in game_title for team in active_game_names)
                    if not is_still_pregame:
                        # Game has started; protect completely. ONLY mark validated, leave original reasoning untouched!
                        mlb_sheet.update_cell(row_idx, 14, "VALIDATED")
                        time.sleep(0.5)
                        continue

                    mlb_sheet.update_cell(row_idx, 14, action)
                    if action == "VALIDATED":
                        if val.get("updated_odds"): mlb_sheet.update_cell(row_idx, 6, int(round(float(val.get("updated_odds")))))
                        if val.get("updated_model_prob"): mlb_sheet.update_cell(row_idx, 8, val.get("updated_model_prob"))
                        if val.get("updated_expected_value"): mlb_sheet.update_cell(row_idx, 9, val.get("updated_expected_value"))
                        if reason: mlb_sheet.update_cell(row_idx, 13, reason)
                        mlb_sheet.update_cell(row_idx, 2, current_time_str)
                    elif action == "REJECTED":
                        mlb_sheet.update_cell(row_idx, 11, "REJECTED")
                        mlb_sheet.update_cell(row_idx, 12, 0.0)
                        if reason: mlb_sheet.update_cell(row_idx, 13, reason)
                        mlb_sheet.update_cell(row_idx, 2, current_time_str)
                    time.sleep(0.5)
            except Exception: pass
