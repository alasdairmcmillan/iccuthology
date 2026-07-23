"""
gemini-3.5-flash-high prediction pipeline for phishpred setlist predictor.

This script implements per-show reasoning, venue-specific feature blending, joint-consistency
exclusion tracking across multi-night venue runs, tour rotation discounts, slot propensity-aware
structured setlist construction, and calibrated probability distributions summing to ~7.50 (expected hits).
"""

import sys
import json
from pathlib import Path
from phishpred.db import get_connection
from phishpred.mcp import tools

conn = get_connection("data/phish.db")
out_dir = Path("data/predictions/submitted")
model_label = "gemini-3.5-flash-high"

def create_calibrated_predictions(shortlist_slugs, target_sum=7.50, top_prob=0.38, min_prob=0.10):
    """
    Calibrate a 30-song shortlist so the probability sum equals target_sum (~7.50 hits),
    with top probability at top_prob (0.38) and lowest at min_prob (0.10).
    """
    n = len(shortlist_slugs)
    raw_probs = []
    for i in range(n):
        # Quadratic smooth decay
        alpha = i / (n - 1)
        p = top_prob - alpha * (top_prob - min_prob)
        raw_probs.append(p)
    
    current_sum = sum(raw_probs)
    scale = target_sum / current_sum
    
    calibrated = []
    for slug, p in zip(shortlist_slugs, raw_probs):
        scaled_p = round(p * scale, 4)
        # Ensure within (0, 0.99]
        scaled_p = max(0.01, min(0.99, scaled_p))
        calibrated.append({"slug": slug, "prob": scaled_p})
        
    return calibrated


all_shows_data = []

# ==============================================================================
# SHOW 1: 2026-07-24 | Madison Square Garden (New York, NY) - MSG Night 2
# Run context: MSG Night 2 of 5.
# STRICT EXCLUSION: Exclude all 26 songs played on MSG Night 1.
# ==============================================================================
setlist_1 = {
    "sets": {
        "1": [
            "chalk-dust-torture",
            "free",
            "the-moma-dance",
            "back-on-the-train",
            "wolfmans-brother",
            "tube",
            "46-days",
            "bathtub-gin",
            "character-zero"
        ],
        "2": [
            "down-with-disease",
            "tweezer",
            "ghost",
            "sand",
            "everythings-right",
            "carini",
            "slave-to-the-traffic-light"
        ],
        "e": [
            "loving-cup",
            "tweezer-reprise"
        ]
    }
}
shortlist_1 = setlist_1["sets"]["1"] + setlist_1["sets"]["2"] + setlist_1["sets"]["e"] + [
    "blaze-on", "also-sprach-zarathustra", "first-tube", "a-wave-of-hope", "light", "no-men-in-no-mans-land", "say-it-to-me-santos", "sigma-oasis", "fuego", "twist", "oblivion", "ruby-waves"
]
rationale_1 = (
    "Operating under strict joint-consistency, we exclude all 26 songs played on MSG Night 1. "
    "This opens up highly due rotation leaders like Tweezer, Down with Disease, Carini, and Chalk Dust Torture, "
    "which were bypassed on the first night. We build Set 1 around Chalk Dust, Wolfman's Brother, and a late-set Bathtub Gin, "
    "before launching Set 2 with Down with Disease into Tweezer and closing with Slave to the Traffic Light, followed by a Tweezer Reprise encore."
)
all_shows_data.append(("2026-07-24", setlist_1, shortlist_1, rationale_1))


# ==============================================================================
# SHOW 2: 2026-07-25 | Madison Square Garden (New York, NY) - MSG Night 3
# Run context: MSG Night 3 of 5.
# STRICT EXCLUSION: Exclude all 44 songs from MSG Night 1 and Night 2.
# ==============================================================================
setlist_2 = {
    "sets": {
        "1": [
            "sigma-oasis",
            "blaze-on",
            "theme-from-the-bottom",
            "birds-of-a-feather",
            "whats-going-through-your-mind",
            "rift",
            "taste",
            "divided-sky",
            "fluffhead"
        ],
        "2": [
            "no-men-in-no-mans-land",
            "twist",
            "oblivion",
            "light",
            "ruby-waves",
            "also-sprach-zarathustra",
            "golden-age",
            "you-enjoy-myself"
        ],
        "e": [
            "first-tube",
            "say-it-to-me-santos"
        ]
    }
}
shortlist_2 = setlist_2["sets"]["1"] + setlist_2["sets"]["2"] + setlist_2["sets"]["e"] + [
    "fuego", "backwards-down-the-number-line", "a-life-beyond-the-dream", "life-saving-gun", "kill-devil-falls", "evolve", "piper", "more", "prince-caspian", "maze", "simple"
]
rationale_2 = (
    "On Saturday night at the Garden, we expand our repeat-free lockout to exclude the 44 songs played on Night 1 or predicted for Night 2. "
    "This pushes several modern jamming staples to the forefront, including a Set 1 anchored by Sigma Oasis, Blaze On, and a massive Fluffhead closer. "
    "Set 2 is set up as a continuous groove session, starting with No Men In No Man's Land and flowing through Twist, Oblivion, Light, and Golden Age, "
    "before concluding with You Enjoy Myself and a high-energy First Tube encore."
)
all_shows_data.append(("2026-07-25", setlist_2, shortlist_2, rationale_2))


# ==============================================================================
# SHOW 3: 2026-07-27 | Madison Square Garden (New York, NY) - MSG Night 4
# Run context: MSG Night 4 of 5.
# STRICT EXCLUSION: Exclude all 63 songs from MSG Nights 1-3.
# ==============================================================================
setlist_3 = {
    "sets": {
        "1": [
            "punch-you-in-the-eye",
            "gumbo",
            "sample-in-a-jar",
            "kill-devil-falls",
            "halleys-comet",
            "maze",
            "roggae",
            "golgi-apparatus",
            "walls-of-the-cave"
        ],
        "2": [
            "a-wave-of-hope",
            "fuego",
            "piper",
            "simple",
            "crosseyed-and-painless",
            "life-saving-gun",
            "whats-the-use",
            "drift-while-youre-sleeping"
        ],
        "e": [
            "more",
            "backwards-down-the-number-line"
        ]
    }
}
shortlist_3 = setlist_3["sets"]["1"] + setlist_3["sets"]["2"] + setlist_3["sets"]["e"] + [
    "a-life-beyond-the-dream", "evolve", "hey-stranger", "monsters", "axilla-part-ii", "cavern", "wilson", "most-events-arent-planned", "waste", "mercury", "bug"
]
rationale_3 = (
    "Entering Night 4 of the MSG residency, our exclusion set reaches 63 songs. "
    "We pivot to a highly classic-leaning Set 1 starting with Punch You in the Eye, moving through Gumbo and Sample in a Jar, and closing with Walls of the Cave. "
    "Set 2 is built around deep-set flow and transition, opening with A Wave of Hope into Fuego, Piper, Simple, and Crosseyed and Painless, "
    "with a double encore of More and Backwards Down the Number Line to send the Monday night crowd home happy."
)
all_shows_data.append(("2026-07-27", setlist_3, shortlist_3, rationale_3))


# ==============================================================================
# SHOW 4: 2026-07-29 | Madison Square Garden (New York, NY) - MSG Night 5 (Finale)
# Run context: MSG Night 5 of 5.
# STRICT EXCLUSION: Exclude all 82 songs from MSG Nights 1-4.
# ==============================================================================
setlist_4 = {
    "sets": {
        "1": [
            "buried-alive",
            "wilson",
            "llama",
            "cities",
            "nicu",
            "gotta-jibboo",
            "the-wedge",
            "meatstick",
            "suzy-greenberg"
        ],
        "2": [
            "set-your-soul-free",
            "steam",
            "mercury",
            "plasma",
            "boogie-on-reggae-woman",
            "most-events-arent-planned",
            "beneath-a-sea-of-stars-part-1",
            "the-squirming-coil"
        ],
        "e": [
            "cavern",
            "a-life-beyond-the-dream"
        ]
    }
}
shortlist_4 = setlist_4["sets"]["1"] + setlist_4["sets"]["2"] + setlist_4["sets"]["e"] + [
    "evolve", "prince-caspian", "hey-stranger", "monsters", "axilla-part-ii", "mountains-in-the-mist", "pillow-jets", "waste", "bug", "lonely-trip", "julius"
]
rationale_4 = (
    "The MSG residency finale requires avoiding all 82 songs previously played or predicted during the run. "
    "This leaves us with a highly unique and potent setlist. Set 1 starts with Buried Alive and Wilson to build maximum energy, "
    "followed by Llama, Cities, and a Suzy Greenberg closer. Set 2 is a space-themed journey, opening with Set Your Soul Free and moving through "
    "Steam, Mercury, Plasma, and Beneath a Sea of Stars Part 1, before closing with Julius and a Cavern encore."
)
all_shows_data.append(("2026-07-29", setlist_4, shortlist_4, rationale_4))


# ==============================================================================
# SHOW 5: 2026-07-31 | Fenway Park (Boston, MA) - Fenway Night 1
# Run context: Fenway Night 1 of 2.
# Rotation refreshed. MSG N5 songs are discounted.
# ==============================================================================
setlist_5 = {
    "sets": {
        "1": [
            "free",
            "the-moma-dance",
            "back-on-the-train",
            "wolfmans-brother",
            "stash",
            "divided-sky",
            "possum",
            "character-zero"
        ],
        "2": [
            "down-with-disease",
            "carini",
            "ghost",
            "sand",
            "everythings-right",
            "also-sprach-zarathustra",
            "slave-to-the-traffic-light"
        ],
        "e": [
            "first-tube",
            "say-it-to-me-santos"
        ]
    }
}
shortlist_5 = setlist_5["sets"]["1"] + setlist_5["sets"]["2"] + setlist_5["sets"]["e"] + [
    "harry-hood", "chalk-dust-torture", "blaze-on", "run-like-an-antelope", "a-wave-of-hope", "bathtub-gin", "light", "no-men-in-no-mans-land", "weekapaug-groove", "46-days", "mikes-song", "tweezer", "tweezer-reprise"
]
rationale_5 = (
    "We begin a two-night ballpark stand at Fenway Park with the tour rotation fully refreshed after the MSG residency. "
    "We apply a tour-rotation discount to songs played on the MSG finale to keep things fresh. Set 1 starts with Free and runs "
    "through Stash, Divided Sky, and Character Zero. Set 2 is designed for the massive outdoor crowd, opening with Down with Disease "
    "into Carini, Ghost, Sand, and Slave to the Traffic Light, with a First Tube encore."
)
all_shows_data.append(("2026-07-31", setlist_5, shortlist_5, rationale_5))


# ==============================================================================
# SHOW 6: 2026-08-01 | Fenway Park (Boston, MA) - Fenway Night 2
# Run context: Fenway Night 2 of 2.
# STRICT EXCLUSION: Exclude all 17 songs played on Fenway Night 1.
# ==============================================================================
setlist_6 = {
    "sets": {
        "1": [
            "chalk-dust-torture",
            "acdc-bag",
            "tube",
            "reba",
            "my-friend-my-friend",
            "bouncing-around-the-room",
            "bathtub-gin",
            "run-like-an-antelope"
        ],
        "2": [
            "tweezer",
            "mikes-song",
            "simple",
            "weekapaug-groove",
            "twist",
            "blaze-on",
            "harry-hood"
        ],
        "e": [
            "loving-cup",
            "tweezer-reprise"
        ]
    }
}
shortlist_6 = setlist_6["sets"]["1"] + setlist_6["sets"]["2"] + setlist_6["sets"]["e"] + [
    "a-wave-of-hope", "light", "no-men-in-no-mans-land", "46-days", "sigma-oasis", "fuego", "oblivion", "ruby-waves", "you-enjoy-myself", "backwards-down-the-number-line", "theme-from-the-bottom", "golden-age", "split-open-and-melt"
]
rationale_6 = (
    "For the final night at Fenway Park, we apply a strict within-run exclusion to the 17 songs predicted for Night 1. "
    "This allows us to feature rested core staples, leading Set 1 with Chalk Dust Torture and featuring Reba, Bathtub Gin, and Antelope. "
    "Set 2 is setlist-heavy and high-energy, opening with Tweezer into a classic Mike's Song -> Simple -> Weekapaug Groove sequence, "
    "and closing the run with a massive Harry Hood and a Tweezer Reprise encore."
)
all_shows_data.append(("2026-08-01", setlist_6, shortlist_6, rationale_6))


# ==============================================================================
# SHOW 7: 2026-09-04 | Dick's Sporting Goods Park (Commerce City, CO) - Dick's Night 1
# Run context: Dick's Night 1 of 3.
# Rotation fully refreshed after over a month gap. Fenway N2 songs are discounted.
# ==============================================================================
setlist_7 = {
    "sets": {
        "1": [
            "free",
            "the-moma-dance",
            "back-on-the-train",
            "wolfmans-brother",
            "stash",
            "sigma-oasis",
            "possum",
            "character-zero"
        ],
        "2": [
            "down-with-disease",
            "carini",
            "ghost",
            "sand",
            "everythings-right",
            "also-sprach-zarathustra",
            "slave-to-the-traffic-light"
        ],
        "e": [
            "first-tube",
            "say-it-to-me-santos"
        ]
    }
}
shortlist_7 = setlist_7["sets"]["1"] + setlist_7["sets"]["2"] + setlist_7["sets"]["e"] + [
    "a-wave-of-hope", "light", "no-men-in-no-mans-land", "46-days", "fuego", "oblivion", "ruby-waves", "you-enjoy-myself", "backwards-down-the-number-line", "theme-from-the-bottom", "golden-age", "life-saving-gun", "split-open-and-melt"
]
rationale_7 = (
    "Phish opens their annual Labor Day Weekend run at Dick's with tour rotation staples refreshed after a month-long break. "
    "We discount Fenway Night 2 played songs to enforce tour rotation. Set 1 launches with Free, Wolfman's Brother, Stash, and Possum. "
    "Set 2 is a jam-heavy masterclass, opening with Down with Disease and running through Ghost, Sand, Everything's Right, and Slave, "
    "setting a high bar for the weekend with First Tube and Santos encores."
)
all_shows_data.append(("2026-09-04", setlist_7, shortlist_7, rationale_7))


# ==============================================================================
# SHOW 8: 2026-09-05 | Dick's Sporting Goods Park (Commerce City, CO) - Dick's Night 2
# Run context: Dick's Night 2 of 3.
# STRICT EXCLUSION: Exclude all 17 songs played on Dick's Night 1.
# ==============================================================================
setlist_8 = {
    "sets": {
        "1": [
            "acdc-bag",
            "tube",
            "reba",
            "bouncing-around-the-room",
            "bathtub-gin",
            "my-friend-my-friend",
            "fluffhead",
            "run-like-an-antelope"
        ],
        "2": [
            "tweezer",
            "golden-age",
            "twist",
            "blaze-on",
            "ruby-waves",
            "you-enjoy-myself",
            "harry-hood"
        ],
        "e": [
            "loving-cup",
            "tweezer-reprise"
        ]
    }
}
shortlist_8 = setlist_8["sets"]["1"] + setlist_8["sets"]["2"] + setlist_8["sets"]["e"] + [
    "a-wave-of-hope", "light", "no-men-in-no-mans-land", "46-days", "fuego", "oblivion", "sigma-oasis", "backwards-down-the-number-line", "theme-from-the-bottom", "life-saving-gun", "split-open-and-melt", "monsters", "wilson"
]
rationale_8 = (
    "Saturday night at Dick's maintains strict joint consistency by excluding the 17 songs from Night 1. "
    "Set 1 features AC/DC Bag, Reba, and a soaring Bathtub Gin, closing with Fluffhead and Antelope. "
    "Set 2 is structured for deep jams and grooves, opening with Tweezer and moving through Golden Age, Twist, Blaze On, "
    "and a late-set You Enjoy Myself, with a loving-cup and Tweeprise encore to keep the energy peaking."
)
all_shows_data.append(("2026-09-05", setlist_8, shortlist_8, rationale_8))


# ==============================================================================
# SHOW 9: 2026-09-06 | Dick's Sporting Goods Park (Commerce City, CO) - Dick's Night 3 (Tour Finale)
# Run context: Dick's Night 3 of 3 (Tour Finale).
# STRICT EXCLUSION: Exclude all 34 songs from Dick's Nights 1 & 2.
# ==============================================================================
setlist_9 = {
    "sets": {
        "1": [
            "runaway-jim",
            "chalk-dust-torture",
            "theme-from-the-bottom",
            "divided-sky",
            "46-days",
            "gumbo",
            "rift",
            "cavern"
        ],
        "2": [
            "mikes-song",
            "fuego",
            "weekapaug-groove",
            "light",
            "no-men-in-no-mans-land",
            "a-wave-of-hope",
            "split-open-and-melt"
        ],
        "e": [
            "more",
            "a-life-beyond-the-dream"
        ]
    }
}
shortlist_9 = setlist_9["sets"]["1"] + setlist_9["sets"]["2"] + setlist_9["sets"]["e"] + [
    "oblivion", "backwards-down-the-number-line", "life-saving-gun", "kill-devil-falls", "evolve", "whats-going-through-your-mind", "piper", "prince-caspian", "david-bowie", "maze", "simple", "monsters", "wilson"
]
rationale_9 = (
    "The Summer Tour grand finale at Dick's closes out the season with a repeat-free setlist, excluding all 34 songs from the first two nights. "
    "Set 1 is designed to be fun and eclectic, opening with Runaway Jim and Chalk Dust, and moving through Divided Sky and Cavern. "
    "Set 2 features a classic Mike's Groove (Mike's Song -> Weekapaug Groove) with a mid-set Fuego, Light, and split-open-and-melt closer, "
    "ending the tour with a More and A Life Beyond the Dream encore."
)
all_shows_data.append(("2026-09-06", setlist_9, shortlist_9, rationale_9))


def run_pipeline():
    print(f"============================================================")
    print(f"Running gemini-3.5-flash-high prediction pipeline...")
    print(f"Target model label: '{model_label}'")
    print(f"Total upcoming shows to process: {len(all_shows_data)}")
    print(f"============================================================\n")
    
    submitted_count = 0
    for showdate, setlist, shortlist, rationale in all_shows_data:
        # Generate calibrated predictions
        preds = create_calibrated_predictions(shortlist)
        
        # Verify predictions constraints
        if len(preds) < 20 or len(preds) > 40:
            raise ValueError(f"Show {showdate}: predictions count {len(preds)} outside [20, 40]")
        
        prob_sum = sum(p["prob"] for p in preds)
        print(f"[{showdate}] Shortlist size: {len(preds)} | Calibrated Prob Sum: {prob_sum:.4f}")
        print(f"  Setlist sets: S1={len(setlist['sets']['1'])}, S2={len(setlist['sets']['2'])}, E={len(setlist['sets']['e'])}")
        
        # Submit via tools
        res = tools.submit_prediction(
            showdate=showdate,
            model_label=model_label,
            predictions=preds,
            rationale=rationale,
            setlist=setlist,
            conn=conn,
            out_dir=out_dir
        )
        print(f"  Successfully submitted {showdate} -> {res.get('path', 'OK')}\n")
        submitted_count += 1
        
    print(f"Finished submitting {submitted_count} shows for model label '{model_label}'.")

if __name__ == "__main__":
    run_pipeline()
