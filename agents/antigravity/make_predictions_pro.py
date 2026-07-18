import os
import sqlite3
import numpy as np
from phishpred.db import get_connection
from phishpred.mcp import tools
from phishpred.probs import renormalize_to_k

def build_predictions_for_all_shows():
    conn = get_connection("data/phish.db")
    shows_res = tools.upcoming_shows(conn, limit=50)
    upcoming = shows_res.get("shows", [])
    
    if not upcoming:
        print("No upcoming shows found.")
        return
        
    print(f"Found {len(upcoming)} upcoming shows to predict.")
    
    # Track predicted setlists chronologically to enforce joint consistency
    predicted_setlists = {}
    
    # Process shows in chronological order
    upcoming = sorted(upcoming, key=lambda x: x["showdate"])
    
    model_label = "gemini-3.1-pro-high"
    
    for i, show in enumerate(upcoming):
        showdate = show["showdate"]
        print(f"\n--- Processing showdate: {showdate} ({show['venue_name']}) ---")
        
        # 1. Get baseline heuristic predictions
        heur = tools.heuristic_prediction(conn, showdate, top=200)
        heur_rows = heur.get("rows", [])
        heur_dict = {r["slug"]: r for r in heur_rows}
        
        # 2. Get run context
        run_ctx = tools.run_context(conn, showdate)
        venue_name = run_ctx.get("venue_name", "")
        city = run_ctx.get("city", "")
        state = run_ctx.get("state", "")
        
        # Identify nights in the same run
        run_nights = run_ctx.get("nights", [])
        
        # Identify already played songs in this run
        played_in_run_slugs = set()
        for night in run_nights:
            if night["played"]:
                for perf in night.get("setlist", []):
                    played_in_run_slugs.add(perf["slug"])
        
        # Simulated played songs from earlier shows in this run
        simulated_played_in_run_slugs = set()
        for night in run_nights:
            n_date = night["showdate"]
            if n_date < showdate and n_date in predicted_setlists:
                simulated_played_in_run_slugs.update(predicted_setlists[n_date])
                
        all_played_in_run = played_in_run_slugs.union(simulated_played_in_run_slugs)
        
        # 3. Previous show on the tour
        prev_show_slugs = set()
        if i > 0:
            prev_showdate = upcoming[i-1]["showdate"]
            if prev_showdate in predicted_setlists:
                prev_show_slugs = set(predicted_setlists[prev_showdate])
                
        # 4. Get candidate features for detailed stats
        features_data = tools.candidate_features(conn, showdate, top=200)
        feat_dict = {r["slug"]: r for r in features_data.get("rows", [])}
        
        # 5. Get venue history
        try:
            vh = tools.venue_history(conn, venue_name, top=100)
            venue_shows = vh.get("total_shows", 0)
            venue_songs = {s["slug"]: s for s in vh.get("songs", [])}
        except Exception:
            venue_shows = 0
            venue_songs = {}
            
        # 6. Compute customized scores
        custom_scores = []
        for slug, heur_row in heur_dict.items():
            base_prob = heur_row["prob"]
            
            run_discount = 1.0
            if slug in all_played_in_run:
                run_discount = 0.0
                
            prev_discount = 1.0
            played_prev = feat_dict.get(slug, {}).get("played_prev_show", 0)
            if played_prev or (slug in prev_show_slugs):
                prev_discount = 0.02
                
            venue_boost = 1.0
            if venue_shows >= 5 and slug in venue_songs:
                venue_play_rate = venue_songs[slug]["play_rate"]
                era_rate = feat_dict.get(slug, {}).get("era_rate", 0.1)
                if era_rate is None:
                    era_rate = 0.1
                if venue_play_rate > era_rate:
                    venue_boost = 1.0 + 0.3 * min(venue_play_rate - era_rate, 0.5)
                    
            score = base_prob * run_discount * prev_discount * venue_boost
            
            if score > 0:
                custom_scores.append({
                    "slug": slug,
                    "song_name": heur_row["song"],
                    "score": score,
                    "base_prob": base_prob,
                    "played_in_run": slug in all_played_in_run,
                    "venue_boost": venue_boost,
                    "is_venue_boosted": venue_boost > 1.0
                })
                
        custom_scores.sort(key=lambda x: x["score"], reverse=True)
        shortlist_candidates = custom_scores[:30]
        shortlist_slugs = [c["slug"] for c in shortlist_candidates]
        
        # 7. Run backtest on shortlist to calibrate probabilities
        try:
            bt = tools.backtest_shortlist(conn, shortlist_slugs, n_shows=20)
            mean_recall = bt.get("mean_recall", 0.40)
        except Exception:
            mean_recall = 0.40
            
        expected_setlist_size = 18.25
        target_sum = mean_recall * expected_setlist_size
        
        scores_array = np.array([c["score"] for c in shortlist_candidates])
        calibrated_probs = renormalize_to_k(scores_array, target_sum, cap=0.35)
        
        predictions = []
        for c, prob in zip(shortlist_candidates, calibrated_probs):
            predictions.append({
                "slug": c["slug"],
                "prob": round(float(prob), 4)
            })
        predictions.sort(key=lambda x: x["prob"], reverse=True)
        
        # 8. Build structured setlist using slot propensities
        prop_data = tools.slot_propensities(conn, shortlist_slugs)
        prop_songs = prop_data.get("songs", {})
        pool = list(shortlist_candidates)
        
        enc_pool = sorted(
            [p for p in pool if prop_songs.get(p["slug"], {}).get("slots", {}).get("encore", 0) > 0.05],
            key=lambda x: prop_songs.get(x["slug"], {}).get("slots", {}).get("encore", 0),
            reverse=True
        )
        encore_slugs = [x["slug"] for x in enc_pool[:2]]
        for p in sorted(pool, key=lambda x: prop_songs.get(x["slug"], {}).get("slots", {}).get("encore", 0), reverse=True):
            if len(encore_slugs) < 2 and p["slug"] not in encore_slugs:
                encore_slugs.append(p["slug"])
        pool = [p for p in pool if p["slug"] not in encore_slugs]
        
        s1_open_song = sorted(pool, key=lambda x: prop_songs.get(x["slug"], {}).get("slots", {}).get("set1-open", 0), reverse=True)[0]
        pool.remove(s1_open_song)
        s2_open_song = sorted(pool, key=lambda x: prop_songs.get(x["slug"], {}).get("slots", {}).get("set2-open", 0), reverse=True)[0]
        pool.remove(s2_open_song)
        s1_close_song = sorted(pool, key=lambda x: prop_songs.get(x["slug"], {}).get("slots", {}).get("set1-close", 0), reverse=True)[0]
        pool.remove(s1_close_song)
        s2_close_song = sorted(pool, key=lambda x: prop_songs.get(x["slug"], {}).get("slots", {}).get("set2-close", 0), reverse=True)[0]
        pool.remove(s2_close_song)
        
        mid_candidates = pool[:12]
        s1_mid_slugs = []
        s2_mid_slugs = []
        for mc in mid_candidates:
            slug = mc["slug"]
            s_slots = prop_songs.get(slug, {}).get("slots", {})
            s1_score = s_slots.get("set1-open", 0) + s_slots.get("set1-mid", 0) + s_slots.get("set1-close", 0)
            s2_score = s_slots.get("set2-open", 0) + s_slots.get("set2-mid", 0) + s_slots.get("set2-close", 0)
            if s1_score > s2_score:
                if len(s1_mid_slugs) < 7: s1_mid_slugs.append(slug)
                else: s2_mid_slugs.append(slug)
            else:
                if len(s2_mid_slugs) < 5: s2_mid_slugs.append(slug)
                else: s1_mid_slugs.append(slug)
                    
        set1 = [s1_open_song["slug"]] + s1_mid_slugs + [s1_close_song["slug"]]
        set2 = [s2_open_song["slug"]] + s2_mid_slugs + [s2_close_song["slug"]]
        encore = encore_slugs
        setlist = {"sets": {"1": set1, "2": set2, "e": encore}}
        
        predicted_setlists[showdate] = set1 + set2 + encore
        
        # 9. Rationale generation for gemini-3.1-pro-high
        nights_in_run_count = len(run_nights)
        run_position_index = next((idx + 1 for idx, n in enumerate(run_nights) if n["showdate"] == showdate), 1)
        venue_short = venue_name.split(" at ")[0].split(" - ")[0]
        
        top_3_names = [feat_dict.get(p["slug"], {}).get("song_name", p["slug"]) for p in predictions[:3]]
        
        boosted_songs = [c["song_name"] for c in shortlist_candidates if c.get("is_venue_boosted", False)]
        
        rationale = f"As gemini-3.1-pro-high, we approach this "
        if nights_in_run_count > 1:
            rationale += f"Night {run_position_index} of {nights_in_run_count} at {venue_short} with strict joint consistency."
        else:
            rationale += f"single-night stop at {venue_short} focusing on venue affinities."
            
        if len(simulated_played_in_run_slugs) > 0:
            rationale += f" We explicitly excluded the {len(simulated_played_in_run_slugs)} songs already simulated for prior nights of this run to avoid repeats."
        elif len(played_in_run_slugs) > 0:
            rationale += f" We zeroed out the {len(played_in_run_slugs)} songs previously played on this run."
            
        if len(prev_show_slugs) > 0 and run_position_index == 1:
            rationale += " Songs from the previous tour stop are heavily discounted per tour rotation base rates."
            
        if len(boosted_songs) > 0:
            rationale += f" We detected elevated historical play rates at this venue and boosted {boosted_songs[0]} accordingly."
            
        rationale += (f" We slot {s1_open_song['song_name']} as a high-propensity set 1 opener, "
                      f"and anchor set 2 with {s2_open_song['song_name']} opening and {s2_close_song['song_name']} closing. "
                      f"Top probabilities go to due staples like {top_3_names[0]} and {top_3_names[1]}.")
        
        # 10. Submit prediction
        res = tools.submit_prediction(
            showdate=showdate,
            model_label=model_label,
            predictions=predictions,
            rationale=rationale,
            setlist=setlist,
            conn=conn,
            out_dir="data/predictions/submitted"
        )
        print(f"Submitted {model_label} for {showdate}: {res['path']}")
        
def main():
    build_predictions_for_all_shows()
        
if __name__ == "__main__":
    main()
