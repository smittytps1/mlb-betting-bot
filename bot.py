def compute_quarter_kelly_units(odds, model_prob_str):
    """
    Quarter-Kelly bankroll management anchored around a 1.0-unit baseline.
    Scales down to 0.5u on thin edges/heavy juice and up to 3.0u on high-conviction edges.
    """
    try:
        prob_val = float(str(model_prob_str).replace('%', '').strip()) / 100.0
        dec_odds = american_to_decimal(odds)
        b = dec_odds - 1.0
        if b <= 0:
            return 1.0
            
        kelly = (b * prob_val - (1.0 - prob_val)) / b
        if kelly <= 0:
            return 0.5
            
        # Scaled so a standard ~5% Kelly edge generates 1.0 unit
        raw_units = (kelly * 0.25) * 80.0
        return max(0.5, min(3.0, round(raw_units, 2)))
    except Exception:
        return 1.0
