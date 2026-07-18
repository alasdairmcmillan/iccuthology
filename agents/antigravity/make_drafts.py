import os
import sqlite3
import numpy as np
import json
from pathlib import Path
from phishpred.db import get_connection
from phishpred.mcp import tools
from phishpred.probs import renormalize_to_k

def build_drafts_for_all_shows():
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
    
    model_label = "gemini-3.5-flash-high"
    draft_dir = Path("tmp/drafts")
    draft_dir.mkdir(parents=True, exist_ok=True)
    
    for i, show in enumerate(upcoming):
        showdate = show["showdate"]
        print(f"\n--- Processing showdate: {showdate} ({show['venue_name']}) ---")
        
        # 1. Get baseline heuristic predictions (top=200)
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
        
        # Identify already played songs in this run (from database)
        played_in_run_slugs = set()
        for night in run_nights:
            if night["played"]:
                for perf in night.get("setlist", []):
                    played_in_run_slugs.add(perf["slug"])
        
        # Also identify simulated played songs from earlier shows in this run (from our own drafts)
        simulated_played_in_run_slugs = set()
        for night in run_nights:
            n_date = night["showdate"]
            if n_date < showdate and n_date in predicted_setlists:
                simulated_played_in_run_slugs.update(predicted_setlists[n_date])
                
        all_played_in_run = played_in_run_slugs.union(simulated_played_in_run_slugs)
        
        print(f"Venue: {venue_name}. Run nights: {len(run_nights)} total.")
        print(f"Total discounted run songs: {len(all_played_in_run)}")

        # 3. Identify previous show on the tour
        prev_show_slugs = set()
        if i > 0:
            prev_showdate = upcoming[i-1]["showdate"]
            if prev_showdate in predicted_setlists:
                prev_show_slugs = set(predicted_setlists[prev_showdate])
        else:
            # For the first show (2026-07-18), the previous show is the last played show in the DB (2026-07-17)
            # Let's query the database to get its setlist
            last_played_rows = conn.execute(
                "select s.slug from performances p join shows sh on p.showid = sh.showid join songs s on p.songid = s.songid where sh.showdate = (select max(showdate) from shows where showdate < ?) order by p.set_label, p.position",
                (showdate,)
            ).fetchall()
            prev_show_slugs = {r[0] for r in last_played_rows}
            print(f"First show on tour, loaded previous show ({len(prev_show_slugs)} songs) from DB")
                
        # 4. Get candidate features for detailed stats
        features_data = tools.candidate_features(conn, showdate, top=200)
        feat_dict = {r["slug"]: r for r in features_data.get("rows", [])}
        
        # 5. Get venue history
        try:
            vh = tools.venue_history(conn, venue_name, top=100)
            venue_shows = vh.get("total_shows", 0)
            venue_songs = {s["slug"]: s for s in vh.get("songs", [])}
        except Exception as e:
            print(f"Venue history lookup failed for {venue_name}: {e}")
            venue_shows = 0
            venue_songs = {}
            
        print(f"Canonical Venue Shows: {venue_shows}")

        # 6. Compute customized scores
        custom_scores = []
        for slug, heur_row in heur_dict.items():
            base_prob = heur_row["prob"]
            
            # Run discount (no repeats in run)
            run_discount = 1.0
            if slug in all_played_in_run:
                run_discount = 0.0
                
            # Previous show discount (2% repeat probability)
            prev_discount = 1.0
            if slug in prev_show_slugs:
                prev_discount = 0.02
                
            # Venue boost
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
                    "venue_boost": venue_boost
                })
                
        # Sort by custom score desc
        custom_scores.sort(key=lambda x: x["score"], reverse=True)
        
        # Select shortlist of 30 songs
        shortlist_candidates = custom_scores[:30]
        shortlist_slugs = [c["slug"] for c in shortlist_candidates]
        
        # 7. Run backtest on shortlist to calibrate probabilities
        try:
            bt = tools.backtest_shortlist(conn, shortlist_slugs, n_shows=20)
            mean_recall = bt.get("mean_recall", 0.40)
        except Exception as e:
            print(f"Backtest failed: {e}")
            mean_recall = 0.40
            
        expected_setlist_size = 18.25
        target_sum = mean_recall * expected_setlist_size
        print(f"Shortlist 20-show backtest recall: {mean_recall:.2f} -> Calibrating sum to: {target_sum:.2f}")
        
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
        
        # Find encore (2 songs)
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
        
        # Find Set 1 opener
        s1_open_song = sorted(
            pool,
            key=lambda x: prop_songs.get(x["slug"], {}).get("slots", {}).get("set1-open", 0),
            reverse=True
        )[0]
        pool.remove(s1_open_song)
        
        # Find Set 2 opener
        s2_open_song = sorted(
            pool,
            key=lambda x: prop_songs.get(x["slug"], {}).get("slots", {}).get("set2-open", 0),
            reverse=True
        )[0]
        pool.remove(s2_open_song)
        
        # Find Set 1 closer
        s1_close_song = sorted(
            pool,
            key=lambda x: prop_songs.get(x["slug"], {}).get("slots", {}).get("set1-close", 0),
            reverse=True
        )[0]
        pool.remove(s1_close_song)
        
        # Find Set 2 closer
        s2_close_song = sorted(
            pool,
            key=lambda x: prop_songs.get(x["slug"], {}).get("slots", {}).get("set2-close", 0),
            reverse=True
        )[0]
        pool.remove(s2_close_song)
        
        # We need 7 mid songs for Set 1 and 5 mid songs for Set 2.
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
        
        # Save predicted setlist for chronological consistency
        all_setlist_slugs = set1 + set2 + encore
        predicted_setlists[showdate] = all_setlist_slugs
        
        # Save raw drafts to draft_dir
        draft_payload = {
            "model_label": model_label,
            "showdate": showdate,
            "predictions": predictions,
            "setlist": setlist,
            "venue_name": venue_name,
            "city": city,
            "state": state,
            "run_nights_count": len(run_nights),
            "run_position_index": next((idx + 1 for idx, n in enumerate(run_nights) if n["showdate"] == showdate), 1),
            "simulated_played_in_run_slugs": list(simulated_played_in_run_slugs),
            "played_in_run_slugs": list(played_in_run_slugs),
            "prev_show_slugs": list(prev_show_slugs)
        }
        
        draft_file = draft_dir / f"{showdate}.json"
        with open(draft_file, "w", encoding="utf-8") as df:
            json.dump(draft_payload, df, indent=2)
        print(f"Saved draft for {showdate} to {draft_file}")

if __name__ == "__main__":
    build_drafts_for_all_shows()
