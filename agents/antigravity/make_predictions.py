import os
import sqlite3
import numpy as np
from phishpred.db import get_connection
from phishpred.mcp import tools
from phishpred.probs import renormalize_to_k

def build_predictions_for_show(conn, showdate, model_label="gemini-3.5-flash-high"):
    print(f"\n--- Processing showdate: {showdate} ---")
    
    # 1. Get baseline heuristic predictions
    heur = tools.heuristic_prediction(conn, showdate)
    heur_rows = heur.get("rows", [])
    heur_dict = {r["slug"]: r for r in heur_rows}
    
    # 2. Get run context
    run_ctx = tools.run_context(conn, showdate)
    venue_name = run_ctx.get("venue_name", "")
    city = run_ctx.get("city", "")
    state = run_ctx.get("state", "")
    
    # Identify songs already played in this run
    played_in_run_slugs = set()
    run_nights = run_ctx.get("nights", [])
    played_nights_count = 0
    for night in run_nights:
        if night["played"]:
            played_nights_count += 1
            for perf in night.get("setlist", []):
                played_in_run_slugs.add(perf["slug"])
                
    print(f"Venue: {venue_name} in {city}, {state}. Run night {len(played_in_run_slugs) > 0 and played_nights_count + 1 or 1}")
    print(f"Songs already played in this run: {len(played_in_run_slugs)}")

    # 3. Get candidate features for detailed stats (e.g. era_rate)
    features_data = tools.candidate_features(conn, showdate, top=150)
    feat_dict = {r["slug"]: r for r in features_data.get("rows", [])}
    
    # 4. Get venue history
    try:
        vh = tools.venue_history(conn, venue_name, top=100)
        venue_shows = vh.get("total_shows", 0)
        venue_songs = {s["slug"]: s for s in vh.get("songs", [])}
    except Exception as e:
        print(f"Venue history lookup failed for {venue_name}: {e}")
        venue_shows = 0
        venue_songs = {}
        
    print(f"Canonical Venue Shows: {venue_shows}")

    # 5. Compute customized scores
    custom_scores = []
    for slug, heur_row in heur_dict.items():
        base_prob = heur_row["prob"]
        
        # Multipliers
        run_discount = 1.0
        if slug in played_in_run_slugs:
            # Exclude run repeats (Monty Hall rule)
            run_discount = 0.0
            
        # Venue boost
        venue_boost = 1.0
        if venue_shows >= 5 and slug in venue_songs:
            venue_play_rate = venue_songs[slug]["play_rate"]
            # Compare to era rate if available
            era_rate = feat_dict.get(slug, {}).get("era_rate", 0.1)
            if era_rate is None:
                era_rate = 0.1
            if venue_play_rate > era_rate:
                # Boost if venue play rate is higher than average era rate
                venue_boost = 1.0 + 0.3 * min(venue_play_rate - era_rate, 0.5)
                
        score = base_prob * run_discount * venue_boost
        custom_scores.append({
            "slug": slug,
            "song_name": heur_row["song"],
            "score": score,
            "base_prob": base_prob,
            "played_in_run": slug in played_in_run_slugs,
            "venue_boost": venue_boost
        })
        
    # Sort by custom score desc
    custom_scores.sort(key=lambda x: x["score"], reverse=True)
    
    # Filter out zero scores
    candidates = [c for c in custom_scores if c["score"] > 0]
    
    # Select shortlist of 30 songs
    shortlist_candidates = candidates[:30]
    shortlist_slugs = [c["slug"] for c in shortlist_candidates]
    
    # 6. Run backtest on shortlist to calibrate probabilities
    try:
        bt = tools.backtest_shortlist(conn, shortlist_slugs, n_shows=20)
        mean_recall = bt.get("mean_recall", 0.40)
    except Exception as e:
        print(f"Backtest failed: {e}")
        mean_recall = 0.40
        
    # Calibration target sum = recall * expected setlist size (approx 18.25)
    expected_setlist_size = 18.25
    target_sum = mean_recall * expected_setlist_size
    print(f"Shortlist 20-show backtest recall: {mean_recall:.2f} -> Calibrating sum to: {target_sum:.2f}")
    
    # Renormalize scores of the shortlist to sum to target_sum
    scores_array = np.array([c["score"] for c in shortlist_candidates])
    calibrated_probs = renormalize_to_k(scores_array, target_sum, cap=0.35) # Cap at 0.35 for calibration safety
    
    predictions = []
    for c, prob in zip(shortlist_candidates, calibrated_probs):
        predictions.append({
            "slug": c["slug"],
            "prob": round(float(prob), 4)
        })
        
    # Ensure sorted by prob descending
    predictions.sort(key=lambda x: x["prob"], reverse=True)
    
    # 7. Build structured setlist using slot propensities
    prop_data = tools.slot_propensities(conn, shortlist_slugs)
    prop_songs = prop_data.get("songs", {})
    
    # Let's assign slots greedily
    pool = list(shortlist_candidates)
    
    # Find encore (2 songs)
    enc_pool = sorted(
        [p for p in pool if prop_songs.get(p["slug"], {}).get("slots", {}).get("encore", 0) > 0.05],
        key=lambda x: prop_songs.get(x["slug"], {}).get("slots", {}).get("encore", 0),
        reverse=True
    )
    encore_slugs = [x["slug"] for x in enc_pool[:2]]
    # fallback if not enough
    for p in sorted(pool, key=lambda x: prop_songs.get(x["slug"], {}).get("slots", {}).get("encore", 0), reverse=True):
        if len(encore_slugs) < 2 and p["slug"] not in encore_slugs:
            encore_slugs.append(p["slug"])
            
    # Remove from pool
    pool = [p for p in pool if p["slug"] not in encore_slugs]
    
    # Find Set 1 opener (1 song)
    s1_open_song = sorted(
        pool,
        key=lambda x: prop_songs.get(x["slug"], {}).get("slots", {}).get("set1-open", 0),
        reverse=True
    )[0]
    pool.remove(s1_open_song)
    
    # Find Set 2 opener (1 song)
    s2_open_song = sorted(
        pool,
        key=lambda x: prop_songs.get(x["slug"], {}).get("slots", {}).get("set2-open", 0),
        reverse=True
    )[0]
    pool.remove(s2_open_song)
    
    # Find Set 1 closer (1 song)
    s1_close_song = sorted(
        pool,
        key=lambda x: prop_songs.get(x["slug"], {}).get("slots", {}).get("set1-close", 0),
        reverse=True
    )[0]
    pool.remove(s1_close_song)
    
    # Find Set 2 closer (1 song)
    s2_close_song = sorted(
        pool,
        key=lambda x: prop_songs.get(x["slug"], {}).get("slots", {}).get("set2-close", 0),
        reverse=True
    )[0]
    pool.remove(s2_close_song)
    
    # We have: s1_open_song, s1_close_song, s2_open_song, s2_close_song, and encore_slugs
    # We need 7 mid songs for Set 1 and 5 mid songs for Set 2.
    # Take the top 12 remaining songs in the pool (by original candidate index, which is sorted by score)
    mid_candidates = pool[:12]
    
    s1_mid_slugs = []
    s2_mid_slugs = []
    for mc in mid_candidates:
        slug = mc["slug"]
        s_slots = prop_songs.get(slug, {}).get("slots", {})
        s1_score = s_slots.get("set1-open", 0) + s_slots.get("set1-mid", 0) + s_slots.get("set1-close", 0)
        s2_score = s_slots.get("set2-open", 0) + s_slots.get("set2-mid", 0) + s_slots.get("set2-close", 0)
        if s1_score > s2_score:
            if len(s1_mid_slugs) < 7:
                s1_mid_slugs.append(slug)
            else:
                s2_mid_slugs.append(slug)
        else:
            if len(s2_mid_slugs) < 5:
                s2_mid_slugs.append(slug)
            else:
                s1_mid_slugs.append(slug)
                
    # Assemble setlist
    set1 = [s1_open_song["slug"]] + s1_mid_slugs + [s1_close_song["slug"]]
    set2 = [s2_open_song["slug"]] + s2_mid_slugs + [s2_close_song["slug"]]
    encore = encore_slugs
    
    setlist = {
        "sets": {
            "1": set1,
            "2": set2,
            "e": encore
        }
    }
    
    # 8. Write a custom rationale specific to this show
    # Rationale must be 2-5 sentences
    venue_short = venue_name.split(" at ")[0].split(" - ")[0]
    
    # Find top 3 songs in predictions
    top_3_names = [feat_dict.get(p["slug"], {}).get("song_name", p["slug"]) for p in predictions[:3]]
    top_3_str = ", ".join(top_3_names)
    
    p_nights_str = ""
    if len(played_in_run_slugs) > 0:
        p_nights_str = f" After discounting the {len(played_in_run_slugs)} songs already played this run at {venue_short},"
    else:
        p_nights_str = f" Opening the run at {venue_short},"
        
    rationale = (
        f"{model_label} predictions for {showdate} in {city}, {state}."
        f"{p_nights_str} we lean on highly due rotation songs: {top_3_str}."
        f" The structured setlist positions key selections like {s1_open_song['song_name']} as Set 1 opener and "
        f"{s2_close_song['song_name']} in the closer role to maximize slot propensity."
    )
    
    # Submit prediction
    res = tools.submit_prediction(
        showdate=showdate,
        model_label=model_label,
        predictions=predictions,
        rationale=rationale,
        setlist=setlist,
        conn=conn,
        out_dir="data/predictions/submitted"
    )
    print(f"Submitted successfully: {res['path']}")
    return res

def main():
    conn = get_connection("data/phish.db")
    shows_res = tools.upcoming_shows(conn, limit=50)
    upcoming = shows_res.get("shows", [])
    
    if not upcoming:
        print("No upcoming shows found.")
        return
        
    print(f"Found {len(upcoming)} upcoming shows to predict.")
    for show in upcoming:
        showdate = show["showdate"]
        build_predictions_for_show(conn, showdate)
        
if __name__ == "__main__":
    main()
