import os
import sqlite3
import numpy as np
from phishpred.db import get_connection
from phishpred.mcp import tools
from phishpred.probs import renormalize_to_k

MY_REASONING = {
    "2026-07-19": {
        "anchors": {"s1-open": "chalk-dust-torture", "s2-open": "down-with-disease", "s2-close": "slave-to-the-traffic-light", "e": "bouncing-around-the-room"},
        "rationale": "Night 2 at Merriweather. Chalk Dust Torture is highly due and fits the Set 1 opener slot perfectly. Down with Disease anchors the second set, with Slave closing it out. We strictly avoided songs played on Night 1 here."
    },
    "2026-07-21": {
        "anchors": {"s1-open": "the-moma-dance", "s2-open": "mikes-song", "s1-close": "weekapaug-groove", "e": "possum"},
        "rationale": "Single night in Syracuse. We expect a classic Mike's Groove pairing, with Moma Dance opening things up. Since this is a one-off show before MSG, we expect high-energy crowd pleasers like Possum in the encore."
    },
    "2026-07-22": {
        "anchors": {"s1-open": "wolfmans-brother", "s2-open": "blaze-on", "s2-close": "also-sprach-zarathustra", "e": "46-days"},
        "rationale": "Night 1 of a massive 5-night MSG run. We start with Wolfman's to get the groove going. Blaze On feels like the perfect Set 2 kickoff. We've discounted recent Syracuse plays to maintain tour rotation."
    },
    "2026-07-24": {
        "anchors": {"s1-open": "carini", "s2-open": "twist", "s2-close": "harry-hood", "e": "a-life-beyond-the-dream"},
        "rationale": "Night 2 at MSG. Carini as an explosive opener sets a dark tone. Harry Hood is overdue to close a big second set. We discount all songs played on MSG Night 1 to maintain run consistency."
    },
    "2026-07-25": {
        "anchors": {"s1-open": "tube", "s2-open": "no-men-in-no-mans-land", "s2-close": "character-zero", "e": "loving-cup"},
        "rationale": "Saturday night at the Garden. Tube brings the funk early, while NMINML is our anchor for Set 2. Loving Cup is the classic Saturday night encore. We ensure strict consistency by avoiding any repeats from Nights 1 and 2."
    },
    "2026-07-27": {
        "anchors": {"s1-open": "punch-you-in-the-eye", "s2-open": "golden-age", "s2-close": "david-bowie", "e": "the-squirming-coil"},
        "rationale": "Night 4 at MSG. We dig a bit deeper into the rotation with PYITE opening and a jam-heavy Golden Age in Set 2. Bowie serves as a dark closer. No repeats from the first three MSG nights."
    },
    "2026-07-29": {
        "anchors": {"s1-open": "fluffhead", "s2-open": "ruby-waves", "s2-close": "split-open-and-melt", "e": "first-tube"},
        "rationale": "The finale of the 5-night MSG run. Fluffhead is the quintessential big-run opener (or closer, but we'll call it to open). We've eliminated all songs played on Nights 1-4, leaving a very specific pool of remaining due songs."
    },
    "2026-07-31": {
        "anchors": {"s1-open": "free", "s2-open": "sand", "s2-close": "you-enjoy-myself", "e": "golgi-apparatus"},
        "rationale": "Night 1 at Fenway Park. Free kicks things off in the stadium setting. We're calling YEM to close out the second set. We account for tour rotation by discounting the MSG finale."
    },
    "2026-08-01": {
        "anchors": {"s1-open": "wilson", "s2-open": "simple", "s2-close": "run-like-an-antelope", "e": "suzy-greenberg"},
        "rationale": "Night 2 at Fenway. Wilson gets the crowd engaged early. Simple provides the Set 2 launchpad. We ensure no repeats from Fenway Night 1."
    },
    "2026-09-04": {
        "anchors": {"s1-open": "first-tube", "s2-open": "ghost", "s2-close": "cavern", "e": "character-zero"},
        "rationale": "Opening night of the annual Dick's run. First Tube brings immediate energy. We lean on the historical high base rates for Dick's and reset the tour rotation since this is a new leg."
    },
    "2026-09-05": {
        "anchors": {"s1-open": "llama", "s2-open": "chalk-dust-torture", "s2-close": "light", "e": "good-times-bad-times"},
        "rationale": "Night 2 at Dick's. A fast Llama opener. Chalk Dust Torture should be ready for a massive Set 2 jam by this point. We strictly discount Dick's Night 1."
    },
    "2026-09-06": {
        "anchors": {"s1-open": "the-curtain-with", "s2-open": "everythings-right", "s2-close": "slave-to-the-traffic-light", "e": "tweezer-reprise"},
        "rationale": "The tour finale at Dick's. We expect some rarities like The Curtain With, and Tweezer Reprise to close the summer. We discount everything played on Nights 1 and 2 at Dick's."
    }
}

def build_predictions_pro():
    conn = get_connection("data/phish.db")
    shows_res = tools.upcoming_shows(conn, limit=50)
    upcoming = sorted(shows_res.get("shows", []), key=lambda x: x["showdate"])
    
    predicted_setlists = {}
    model_label = "gemini-3.1-pro-high"
    
    for i, show in enumerate(upcoming):
        showdate = show["showdate"]
        print(f"\\n--- Processing showdate: {showdate} ({show['venue_name']}) ---")
        
        my_reasoning = MY_REASONING.get(showdate, {})
        my_anchors = my_reasoning.get("anchors", {})
        rationale = my_reasoning.get("rationale", f"Model reasoning for {showdate}.")
        
        heur = tools.heuristic_prediction(conn, showdate, top=300)
        heur_rows = heur.get("rows", [])
        heur_dict = {r["slug"]: r for r in heur_rows}
        
        run_ctx = tools.run_context(conn, showdate)
        run_nights = run_ctx.get("nights", [])
        
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
                
        features_data = tools.candidate_features(conn, showdate, top=300)
        feat_dict = {r["slug"]: r for r in features_data.get("rows", [])}
        
        custom_scores = []
        for slug, heur_row in heur_dict.items():
            base_prob = heur_row["prob"]
            run_discount = 0.0 if slug in all_played_in_run else 1.0
            
            played_prev = feat_dict.get(slug, {}).get("played_prev_show", 0)
            prev_discount = 0.02 if (played_prev or slug in prev_show_slugs) else 1.0
            
            score = base_prob * run_discount * prev_discount
            
            if slug in my_anchors.values():
                score += 1.0 # Guarantee inclusion
                
            if score > 0:
                custom_scores.append({
                    "slug": slug,
                    "song_name": heur_row["song"],
                    "score": score,
                    "base_prob": base_prob
                })
                
        custom_scores.sort(key=lambda x: x["score"], reverse=True)
        
        shortlist_candidates = custom_scores[:30]
        
        # Make sure our anchors made it, if not force them
        shortlist_slugs = [c["slug"] for c in shortlist_candidates]
        for role, slug in my_anchors.items():
            if slug not in shortlist_slugs:
                shortlist_candidates.append({
                    "slug": slug,
                    "song_name": slug.replace('-', ' ').title(),
                    "score": 1.0,
                    "base_prob": 0.2
                })
        
        shortlist_candidates = shortlist_candidates[:30] # Trim back to 30 just in case
        shortlist_slugs = [c["slug"] for c in shortlist_candidates]
        
        # Calibrate probabilities
        try:
            bt = tools.backtest_shortlist(conn, shortlist_slugs, n_shows=20)
            mean_recall = bt.get("mean_recall", 0.40)
        except:
            mean_recall = 0.40
            
        target_sum = mean_recall * 18.25
        scores_array = np.array([c["score"] for c in shortlist_candidates])
        calibrated_probs = renormalize_to_k(scores_array, target_sum, cap=0.35)
        
        predictions = []
        for c, prob in zip(shortlist_candidates, calibrated_probs):
            if c["slug"] in my_anchors.values():
                # Anchors get a healthy probability
                prob = max(prob, 0.25)
            predictions.append({
                "slug": c["slug"],
                "prob": round(float(prob), 4)
            })
            
        predictions.sort(key=lambda x: x["prob"], reverse=True)
        
        # Build structured setlist
        prop_data = tools.slot_propensities(conn, shortlist_slugs)
        prop_songs = prop_data.get("songs", {})
        
        pool = list(shortlist_candidates)
        
        def pick_and_remove(role_key, default_prop_key):
            target_slug = my_anchors.get(role_key)
            if target_slug:
                for p in pool:
                    if p["slug"] == target_slug:
                        pool.remove(p)
                        return p["slug"]
            # Fallback
            best = sorted(pool, key=lambda x: prop_songs.get(x["slug"], {}).get("slots", {}).get(default_prop_key, 0), reverse=True)
            if best:
                pool.remove(best[0])
                return best[0]["slug"]
            return pool.pop()["slug"]
            
        s1_open = pick_and_remove("s1-open", "set1-open")
        s2_open = pick_and_remove("s2-open", "set2-open")
        s1_close = pick_and_remove("s1-close", "set1-close")
        s2_close = pick_and_remove("s2-close", "set2-close")
        e1 = pick_and_remove("e", "encore")
        
        s1_mid_slugs = []
        s2_mid_slugs = []
        for mc in pool[:12]:
            s1_mid_slugs.append(mc["slug"]) if len(s1_mid_slugs) < 7 else s2_mid_slugs.append(mc["slug"])
            
        set1 = [s1_open] + s1_mid_slugs + [s1_close]
        set2 = [s2_open] + s2_mid_slugs + [s2_close]
        encore = [e1]
        
        setlist = {
            "sets": {
                "1": set1,
                "2": set2,
                "e": encore
            }
        }
        
        predicted_setlists[showdate] = set1 + set2 + encore
        
        res = tools.submit_prediction(
            showdate=showdate,
            model_label=model_label,
            predictions=predictions,
            rationale=rationale,
            setlist=setlist,
            conn=conn,
            out_dir="data/predictions/submitted"
        )
        print(f"Submitted {showdate} -> {res['path']}")

if __name__ == "__main__":
    build_predictions_pro()
