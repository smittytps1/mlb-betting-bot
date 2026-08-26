def match_canonical_team(name_str):
    if not name_str: return ""
    cleaned = str(name_str).strip().lower()
    cleaned_norm = normalize_text(cleaned)
    
    for canonical, aliases in MLB_TEAM_ALIASES.items():
        for alias in aliases:
            # 1. Exact text match
            if alias == cleaned:
                return canonical.title()
            
            # 2. Exact normalized match (e.g., "o's" correctly matches "os" if passed exactly)
            if normalize_text(alias) == cleaned_norm:
                return canonical.title()
            
            # 3. Substring match ONLY if the alias is 4 letters or longer 
            # (Prevents "os" from triggering on "boston" or "houston")
            if len(alias) >= 4 and (alias in cleaned or cleaned in alias):
                return canonical.title()
                
    return name_str.strip().title()
