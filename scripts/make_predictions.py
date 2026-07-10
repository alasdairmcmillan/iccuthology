"""Batch predictor authored by the Antigravity CLI (gemini-3.5-flash-high)
driving the phishpred MCP tool layer directly. Committed VERBATIM below this
docstring -- it is the frozen strategy that generated every submission in
data/predictions/submitted/gemini-3-5-flash-high/, so its logic must not be
edited after the fact. Unlike the claude-fable/claude-sonnet submissions
(per-show agent reasoning), this is a static formula applied to all upcoming
shows in one pass.

Known quirk, deliberately left as-is: the "venue affinity boost" fires for any
venue_gap > 0, which includes features.VENUE_GAP_SENTINEL (999 = never played
at the venue), so it multiplies nearly every candidate by 1.15 -- a ranking
near-no-op rather than real venue affinity. A corrected strategy should be
submitted under a new model_label rather than by editing this file.

Re-running overwrites submitted/{label}/{date}.json for shows >= today and
refreshes their submitted_at stamps -- avoid once pre-show freeze times matter
for accuracy scoring.
"""
import sqlite3
import json
import os
from pathlib import Path
from phishpred import predict
from phishpred.mcp import tools

def main():
    db_path = Path("data/phish.db")
    if not db_path.exists():
        print("Database not found!")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    upcoming = predict.upcoming_shows(conn, limit=100)
    print(f"Found {len(upcoming)} upcoming shows.")

    # We need to track predictions we make for the same runs to avoid same-run repeats.
    # Group shows by run (venue name/city and contiguous dates, or we can use tools.run_context).
    # Since we can query run_context(showdate), let's use that to see all nights in the run.
    
    # Let's map run information.
    # We will iterate through shows chronologically.
    # Keep track of what we predicted with high probability (> 0.08) in the current run.
    run_predictions = {} # run_id -> set of slugs predicted in this run

    # Let's define the model label
    model_label = "gemini-3.5-flash-high"
    out_dir = Path("data/predictions/submitted")
    out_dir.mkdir(parents=True, exist_ok=True)

    # We will submit predictions for each show.
    for show_idx, show in enumerate(upcoming):
        showdate = show["showdate"]
        venue_name = show["venue_name"]
        print(f"\nProcessing {showdate} - {venue_name}...")

        # 1. Get candidate features and run context
        try:
            cf = tools.candidate_features(conn, showdate, top=100)
            rows = cf["rows"]
        except Exception as e:
            print(f"Error fetching features for {showdate}: {e}")
            continue

        try:
            rc = tools.run_context(conn, showdate)
            nights = rc["nights"]
        except Exception as e:
            print(f"Error fetching run context for {showdate}: {e}")
            nights = []

        # Find target night's position in this run
        target_idx = -1
        for idx, n in enumerate(nights):
            if n["showdate"] == showdate:
                target_idx = idx
                break

        # Determine run ID (e.g. venue name + first showdate of run)
        if nights:
            run_id = f"{rc['venue_name']}_{nights[0]['showdate']}"
        else:
            run_id = f"{venue_name}_{showdate}"

        if run_id not in run_predictions:
            run_predictions[run_id] = {}

        # Get songs already played in this run (from run context)
        played_in_run_so_far = set()
        for n in nights[:target_idx]:
            if n["played"] and "setlist" in n:
                for song in n["setlist"]:
                    played_in_run_so_far.add(song["slug"])

        # Also get what we have predicted with high probability in prior nights of this same run
        predicted_in_run_so_far = set()
        for prev_date, preds in run_predictions[run_id].items():
            # If we predicted a song with prob > 0.08 in a previous night of this run,
            # we should heavily discount it for this night.
            for p in preds:
                if p["prob"] > 0.08:
                    predicted_in_run_so_far.add(p["slug"])

        # 2. Score candidate songs
        predictions = []
        
        # We want to select 25-30 songs.
        # Let's iterate through candidate features.
        # Candidate features are sorted by decayed_rate descending.
        for row in rows:
            slug = row["slug"]
            decayed_rate = row["decayed_rate"] or 0.0
            gap = row["gap"]
            gap_ratio = row["gap_ratio"] or 1.0
            played_prev_show = row["played_prev_show"]
            played_in_run_feature = row["played_in_run"]
            venue_gap = row["venue_gap"]
            plays_this_tour = row["plays_this_tour"] or 0
            is_original = row["is_original"]

            # Start with decayed_rate as baseline probability
            prob = decayed_rate

            # Adjustments:
            # Rule 1: No repeats in run (played already or predicted in run)
            if slug in played_in_run_so_far or played_in_run_feature == 1:
                prob = 0.01
            elif slug in predicted_in_run_so_far:
                prob = 0.02
            # Rule 2: Played in previous show
            elif played_prev_show == 1:
                prob = 0.02
            else:
                # Apply due-ness boost
                if gap_ratio > 1.5:
                    prob *= 1.4
                elif gap_ratio > 1.2:
                    prob *= 1.2
                elif gap_ratio < 0.6:
                    prob *= 0.7 # not due yet

                # Venue affinity boost
                if venue_gap is not None and venue_gap > 0:
                    prob *= 1.15

                # Adjust for tour rotation
                if plays_this_tour > 2:
                    prob *= 0.85 # slightly overplayed this tour
                elif plays_this_tour == 0:
                    prob *= 1.1 # fresh for the tour

            # Clamp probability to (0, 1]
            prob = max(0.01, min(0.99, prob))
            predictions.append({"slug": slug, "prob": prob, "song_name": row["song_name"]})

        # Sort and select top 28 songs
        predictions.sort(key=lambda x: x["prob"], reverse=True)
        shortlist = predictions[:28]

        # Record this show's predictions to avoid repeats in future nights of the same run
        run_predictions[run_id][showdate] = [{"slug": s["slug"], "prob": s["prob"]} for s in shortlist]

        # Prepare payload predictions (exclude song_name, just slug and prob)
        submit_preds = [{"slug": s["slug"], "prob": round(s["prob"], 4)} for s in shortlist]

        # Write a concise rationale
        # Find some key calls (top song, overdue song, and run context)
        top_picks = [s["song_name"] for s in shortlist[:3]]
        overdue_picks = [row["song_name"] for row in rows if (row["gap_ratio"] or 0) > 1.5 and row["played_prev_show"] == 0 and row["played_in_run"] == 0][:2]
        
        rationale = f"Top picks: {', '.join(top_picks)}. "
        if overdue_picks:
            rationale += f"Boosting overdue selections: {', '.join(overdue_picks)}. "
        if played_in_run_so_far:
            rationale += f"Discounting same-run repeats (e.g. {list(played_in_run_so_far)[:2]})."
        else:
            rationale += "Fresh venue run starts tonight."

        # Submit prediction
        try:
            res = tools.submit_prediction(
                showdate=showdate,
                model_label=model_label,
                predictions=submit_preds,
                rationale=rationale,
                conn=conn,
                out_dir=out_dir
            )
            print(f"Submitted predictions for {showdate}. Saved to {res['path']}")
        except Exception as e:
            print(f"Failed to submit predictions for {showdate}: {e}")

    conn.close()

if __name__ == "__main__":
    main()
