import os
import json
from pathlib import Path
from phishpred.db import get_connection
from phishpred.mcp import tools

def main():
    conn = get_connection("data/phish.db")
    draft_dir = Path("tmp/drafts")
    out_dir = Path("data/predictions/submitted")
    model_label = "gemini-3.5-flash-high"
    
    rationales = {
        "2026-07-18": "We kick off the two-night run at Merriweather Post Pavilion with a highly classic-leaning selection. Since key tour workhorses like Harry Hood, Fuego, Golgi, and Llama were played just last night in Raleigh, they are heavily discounted to respect tour rotation. Instead, we anchor the first night around an open Runaway Jim, followed by a strong Set 2 opening Down with Disease, transitioning into the classic Mike's Song -> Weekapaug Groove pairing, and closing Set 2 with a monumental You Enjoy Myself.",
        
        "2026-07-19": "On the second night of the Columbia run, we apply a strict discount to all 18 songs from our Night 1 setlist call. This brings rested staples to the forefront, including a Martian Monster Set 1 opener, and a classic Reba and Bathtub Gin to build momentum. The second set features a deep Oblivion jam leading into Light and Golden Age, with a marquee Harry Hood slot to close out the run in style.",
        
        "2026-07-21": "A single-night tour stop in Syracuse at the Lakeview Amphitheater provides an opportunity for a high-energy, standard-length tour show. We discount all songs played on the second night of the Merriweather run to enforce tour rotation. We call a classic AC/DC Bag Set 1 opener and a late-set Ghost to round out Set 1. Set 2 is built around a heavy Mike's Groove (Mike's Song -> Weekapaug Groove) with Sand and You Enjoy Myself in the mid-to-late section, capped by a First Tube encore.",
        
        "2026-07-22": "We begin a massive five-night run at Madison Square Garden. For Night 1, we start fresh with no run discounts, but we apply a previous-show rotation discount on songs from Syracuse. Set 1 is designed to be thematic and classic, starting with Runaway Jim and working through Maze, Rift, Roggae, and David Bowie. Set 2 opens with a sprawling Set Your Soul Free and builds through Oblivion, Prince Caspian, and a set-closing Suzy Greenberg, leaving plenty of heavy hitters for the remaining nights.",
        
        "2026-07-24": "Entering Night 2 of the MSG run, we discount all 18 songs called in our Night 1 MSG setlist. To set a dynamic tone, we lead Set 1 with AC/DC Bag and feature a mid-set Llama and The Squirming Coil. Set 2 is a jamming journey, opening with Down with Disease and running through Gotta Jibboo, Whats the Use, and Beneath a Sea of Stars Part 1, culminating in a beautiful Everything's Right. We finish the night with a double encore of Slave and Julius.",
        
        "2026-07-25": "Halfway through the five-night MSG run, we expand our exclusion set to 36 songs from the first two nights. We call a high-energy Set 1 starting with Buried Alive and Punch You in the Eye, moving through Steam and Gumbo, and closing with Walls of the Cave. Set 2 features a Chalk Dust Torture opener, transitioning into I Am Hydrogen, Boogie On Reggae Woman, and Mercury, wrapping up with a Possum encore.",
        
        "2026-07-27": "For Night 4 at MSG, 54 songs are now excluded from our pool. We design a unique setlist focused on different textures, opening Set 1 with Free and featuring Ocelot, Timber, and Blaze On. The second set is anchored by No Men In No Man's Land and Sand, running through Moma Dance, Stash, and Ghost, before closing with a marquee Harry Hood and a First Tube encore.",
        
        "2026-07-29": "The finale of the MSG run requires avoiding all 72 songs called across the first four nights. The remaining pool gives us a highly unique and potent setlist. Set 1 features a heavy Mike's Groove (Mike's -> Weekapaug) and closes with a big Bathtub Gin. Set 2 opens with Rock and Roll, transitions into Also Sprach Zarathustra and a massive late-run Tweezer, before closing with You Enjoy Myself and a Character Zero encore.",
        
        "2026-07-31": "We arrive in Boston for a two-night run at Fenway Park. We start fresh with no run discounts, though we discount songs played on the MSG finale to respect tour rotation. Set 1 starts with AC/DC Bag and runs through My Friend, My Friend and Run-Like-An-Antelope. Set 2 is built for a ballpark crowd, opening with Oblivion and transitioning into Harry Hood, Ghost, Ruby Waves, and a set-closing Slave to the Traffic Light, with a First Tube encore.",
        
        "2026-08-01": "The final night at Fenway Park discounts all 18 songs from our Night 1 predictions. This allows us to feature rested staples like Buried Alive to open Set 1, followed by a mid-set Maze, Fluffhead, and Divided Sky. Set 2 is setlist-heavy, opening with Mike's Song and transitioning into Twist, Weekapaug Groove, and Life Saving Gun, closing the run with a Suzy Greenberg-style energy and a More encore.",
        
        "2026-09-04": "We begin the traditional three-night end-of-summer run at Dick's Sporting Goods Park. We discount songs from the Fenway Park finale to respect tour rotation. Set 1 opens with AC/DC Bag and features Reba and David Bowie. Set 2 is a jam-heavy masterclass, opening with Down with Disease and running through Ghost, 2001, Fuego, A Wave of Hope, and Piper, culminating in a marquee Harry Hood to set the bar high for the weekend.",
        
        "2026-09-05": "For Night 2 at Dick's, we discount all 18 songs from Night 1. This rotation brings Buried Alive to open Set 1, along with Divided Sky and Golgi. Set 2 is structured around a classic Mike's Groove (Mike's Song -> Weekapaug Groove) with a mid-set Simple and Llama, closing with Suzy Greenberg and a Julius encore to keep the energy peaking.",
        
        "2026-09-06": "The final night of the Dick's run and the end-of-summer tour. We exclude the 36 songs played on the first two nights of the run. Set 1 is designed to be fun and eclectic, opening with Punch You in the Eye and running through Halley's Comet, Cities, Gumbo, and Sand. Set 2 features a Chalk Dust Torture opener, moving into The Lizards and Beneath a Sea of Stars Part 1, closing the tour with a Squirming Coil encore."
    }

    for showdate, rationale in rationales.items():
        draft_file = draft_dir / f"{showdate}.json"
        if not draft_file.exists():
            print(f"[-] Draft not found for {showdate}")
            continue
            
        with open(draft_file, "r", encoding="utf-8") as df:
            draft = json.load(df)
            
        predictions = draft["predictions"]
        setlist = draft["setlist"]
        
        # Make adjustments:
        # 1. Ensure mikes-song -> weekapaug-groove sequence in setlist if both present in a set
        for s_key in ["1", "2", "e"]:
            songs = setlist["sets"].get(s_key, [])
            if "mikes-song" in songs and "weekapaug-groove" in songs:
                # order mikes-song before weekapaug-groove
                m_idx = songs.index("mikes-song")
                w_idx = songs.index("weekapaug-groove")
                if m_idx > w_idx:
                    songs[m_idx], songs[w_idx] = songs[w_idx], songs[m_idx]
                    print(f"[{showdate}] Swapped mikes-song and weekapaug-groove in Set {s_key}")
                    
        # Submit prediction
        res = tools.submit_prediction(
            showdate=showdate,
            model_label=model_label,
            predictions=predictions,
            rationale=rationale,
            setlist=setlist,
            conn=conn,
            out_dir=str(out_dir)
        )
        print(f"[+] Submitted {showdate} -> {res['path']}")

if __name__ == "__main__":
    main()
