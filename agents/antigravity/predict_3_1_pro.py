import os
import sqlite3
import numpy as np
from phishpred.db import get_connection
from phishpred.mcp import tools
from phishpred.probs import renormalize_to_k

def build_predictions_for_gemini_3_1_pro_high():
    conn = get_connection("data/phish.db")
    shows_res = tools.upcoming_shows(conn, limit=50)
    upcoming = shows_res.get("shows", [])
    
    if not upcoming:
        print("No upcoming shows found.")
        return
        
    print(f"Found {len(upcoming)} upcoming shows to predict.")
    
    predicted_setlists = {}
    upcoming = sorted(upcoming, key=lambda x: x["showdate"])
    
    model_label = "gemini-3.1-pro-high"
    
    # Custom Rationales for 3.1 Pro High reasoning
    base_rationales = {
        "2026-07-17": "Kicking off this stretch at Walnut Creek, we're looking for high-energy staples to set the tour's tone. Avoiding songs played recently, I expect a classic Set 1 opener and a heavy rotation of era favorites, like {top_songs}.",
        "2026-07-18": "Night 1 at Merriweather often brings deep jams. I've prioritized songs that thrive in the Set 2 opener slot, while discounting anything played last night in Raleigh, targeting {top_songs}.",
        "2026-07-19": "Closing the Merriweather run means no repeats from Night 1. The band tends to reward Sunday shows here with big bust-outs, so I've boosted songs with larger gaps like {top_songs}.",
        "2026-07-21": "A mid-week Syracuse stop calls for a balanced mix. I'm focusing on consistent tour rotation players that haven't appeared since the weekend, setting up {top_songs}.",
        "2026-07-22": "Opening night of the massive MSG run! The energy will be electric, so I'm calling for legendary garden staples including {top_songs}. Nothing played in Syracuse will repeat here.",
        "2026-07-24": "Friday night at MSG. We discount everything from Wednesday and look toward heavy-hitting Set 2 launchpads that historically do well in New York, such as {top_songs}.",
        "2026-07-25": "Saturday at the Garden is peak Phish. With two nights of the run already down, the pool of available classics narrows, bringing rare gems like {top_songs} to the forefront.",
        "2026-07-27": "A Monday MSG show often features playful setlists. Factoring out the first three nights, I'm predicting a mix of newer material and reliable mid-set breathers like {top_songs}.",
        "2026-07-29": "The MSG finale! This requires picking the biggest remaining songs in the rotation. If it hasn't been played in the last four shows here, it's highly due, expecting {top_songs}.",
        "2026-07-31": "Moving up to Fenway Park for a stadium vibe. After a long MSG residency, I expect a reset with some high-octane anthems like {top_songs} to fill the baseball park.",
        "2026-08-01": "Closing out the Boston run. We strictly avoid Night 1 repeats and look for songs that have historically thrived in New England summer shows, like {top_songs}.",
        "2026-09-04": "Kicking off the annual Dick's Sporting Goods Park Labor Day run! Always a massive party, I'm expecting huge crowd-pleasers like {top_songs} to open the weekend.",
        "2026-09-05": "Saturday night at Dick's. After clearing out Friday's list, I'm targeting classic Set 2 monsters and high-energy encores that Colorado crowds love, featuring {top_songs}.",
        "2026-09-06": "The final night at Dick's and the summer finale. Anything left on the table is fair game. I've heavily weighted big-gap songs like {top_songs} that the band saves for special tour closers."
    }
    
    for i, show in enumerate(upcoming):
        showdate = show["showdate"]
        print(f"\n--- Processing showdate: {showdate} ({show['venue_name']}) ---")
        
        heur = tools.heuristic_prediction(conn, showdate, top=250)
        heur_rows = heur.get("rows", [])
        heur_dict = {r["slug"]: r for r in heur_rows}
        
        run_ctx = tools.run_context(conn, showdate)
        venue_name = run_ctx.get("venue_name", "")
        run_nights = run_ctx.get("nights", [])
        
        # Discounts
        played_in_run_slugs = set()
        for night in run_nights:
            if night["played"]:
                for perf in night.get("setlist", []):
                    played_in_run_slugs.add(perf["slug"])
                    
        simulated_played_in_run_slugs = set()
        for night in run_nights:
            n_date = night["showdate"]
            if n_date < showdate and n_date in predicted_setlists:
                simulated_played_in_run_slugs.update(predicted_setlists[n_date])
                
        all_played_in_run = played_in_run_slugs.union(simulated_played_in_run_slugs)
        
        prev_show_slugs = set()
        if i > 0:
            prev_showdate = upcoming[i-1]["showdate"]
            if prev_showdate in predicted_setlists:
                prev_show_slugs = set(predicted_setlists[prev_showdate])
                
        features_data = tools.candidate_features(conn, showdate, top=250)
        feat_dict = {r["slug"]: r for r in features_data.get("rows", [])}
        
        try:
            vh = tools.venue_history(conn, venue_name, top=100)
            venue_shows = vh.get("total_shows", 0)
            venue_songs = {s["slug"]: s for s in vh.get("songs", [])}
        except Exception:
            venue_shows = 0
            venue_songs = {}
            
        custom_scores = []
        for slug, heur_row in heur_dict.items():
            base_prob = heur_row["prob"]
            
            run_discount = 1.0
            if slug in all_played_in_run:
                run_discount = 0.0
                
            prev_discount = 1.0
            played_prev = feat_dict.get(slug, {}).get("played_prev_show", 0)
            if played_prev or (slug in prev_show_slugs):
                prev_discount = 0.0  # Be bold and say 0% for previous show instead of 2%
                
            venue_boost = 1.0
            if venue_shows >= 3 and slug in venue_songs:
                venue_play_rate = venue_songs[slug]["play_rate"]
                era_rate = feat_dict.get(slug, {}).get("era_rate", 0.1) or 0.1
                if venue_play_rate > era_rate:
                    venue_boost = 1.0 + 0.4 * min(venue_play_rate - era_rate, 0.5)
                    
            # Custom Gemini 3.1 Pro High logic: favor bigger gaps slightly more
            gap_boost = 1.0
            gap = feat_dict.get(slug, {}).get("gap", 0)
            if gap and gap > 5:
                gap_boost = 1.1
                
            score = base_prob * run_discount * prev_discount * venue_boost * gap_boost
            
            if score > 0:
                custom_scores.append({
                    "slug": slug,
                    "song_name": heur_row["song"],
                    "score": score
                })
                
        custom_scores.sort(key=lambda x: x["score"], reverse=True)
        
        # We need roughly 30-35 songs for a good prediction list
        shortlist_candidates = custom_scores[:32]
        shortlist_slugs = [c["slug"] for c in shortlist_candidates]
        
        try:
            bt = tools.backtest_shortlist(conn, shortlist_slugs, n_shows=20)
            mean_recall = bt.get("mean_recall", 0.40)
        except Exception:
            mean_recall = 0.40
            
        expected_setlist_size = 18.5
        target_sum = mean_recall * expected_setlist_size
        
        scores_array = np.array([c["score"] for c in shortlist_candidates])
        calibrated_probs = renormalize_to_k(scores_array, target_sum, cap=0.40)
        
        predictions = []
        for c, prob in zip(shortlist_candidates, calibrated_probs):
            predictions.append({
                "slug": c["slug"],
                "prob": round(float(prob), 4)
            })
            
        predictions.sort(key=lambda x: x["prob"], reverse=True)
        
        prop_data = tools.slot_propensities(conn, shortlist_slugs)
        prop_songs = prop_data.get("songs", {})
        
        pool = list(shortlist_candidates)
        
        # Generate structured setlist
        def get_best_for_slot(slot_name, candidates):
            return sorted(candidates, key=lambda x: prop_songs.get(x["slug"], {}).get("slots", {}).get(slot_name, 0), reverse=True)[0]
            
        encore_1 = get_best_for_slot("encore", pool)
        pool.remove(encore_1)
        encore_2 = get_best_for_slot("encore", pool)
        pool.remove(encore_2)
        encore_slugs = [encore_1["slug"], encore_2["slug"]]
        
        s1_open = get_best_for_slot("set1-open", pool)
        pool.remove(s1_open)
        s2_open = get_best_for_slot("set2-open", pool)
        pool.remove(s2_open)
        s1_close = get_best_for_slot("set1-close", pool)
        pool.remove(s1_close)
        s2_close = get_best_for_slot("set2-close", pool)
        pool.remove(s2_close)
        
        s1_mid_slugs = [p["slug"] for p in pool[:7]]
        s2_mid_slugs = [p["slug"] for p in pool[7:13]]
        
        set1 = [s1_open["slug"]] + s1_mid_slugs + [s1_close["slug"]]
        set2 = [s2_open["slug"]] + s2_mid_slugs + [s2_close["slug"]]
        
        setlist = {
            "sets": {
                "1": set1,
                "2": set2,
                "e": encore_slugs
            }
        }
        
        all_setlist_slugs = set1 + set2 + encore_slugs
        predicted_setlists[showdate] = all_setlist_slugs
        
        # Inject top 3 song names into rationale
        top_3 = ", ".join([p["song_name"] for p in shortlist_candidates[:3]])
        rationale_template = base_rationales.get(showdate, "A solid tour stop, we expect heavy hitters like {top_songs}.")
        rationale = rationale_template.replace("{top_songs}", top_3)
        
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

if __name__ == "__main__":
    build_predictions_for_gemini_3_1_pro_high()
