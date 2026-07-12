import sqlite3
from collections import Counter
from phishpred.db import get_connection

def main():
    conn = get_connection("data/phish.db")
    
    # 1. Print tables & schemas
    print("=== TABLES ===")
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    for t in tables:
        tname = t["name"]
        print(f"Table: {tname}")
        cols = conn.execute(f"PRAGMA table_info({tname})").fetchall()
        print("  Columns:", [c["name"] for c in cols])
    
    # 2. Songs that repeat more than once in a single show (same night)
    print("\n=== SONGS PLAYED MORE THAN ONCE IN A SINGLE SHOW ===")
    repeats = conn.execute("""
        SELECT p.showid, s.showdate, v.name AS venue_name, sg.slug, sg.name, COUNT(*) as plays
        FROM performances p
        JOIN shows s ON p.showid = s.showid
        JOIN venues v ON s.venueid = v.venueid
        JOIN songs sg ON p.songid = sg.songid
        WHERE s.exclude = 0
        GROUP BY p.showid, p.songid
        HAVING plays > 1
        ORDER BY plays DESC, s.showdate DESC
        LIMIT 30
    """).fetchall()
    for r in repeats:
        print(f"{r['showdate']} | {r['venue_name']} | {r['name']} ({r['slug']}) - played {r['plays']} times")

    # 3. Song co-occurrences (which songs are played in the same show most frequently)
    print("\n=== TOP SONG PAIR CO-OCCURRENCES (LAST 5 YEARS OF PLAYED SHOWS) ===")
    # Find shows in last 5 years
    recent_shows = conn.execute("""
        SELECT showid FROM shows 
        WHERE exclude = 0 AND show_index IS NOT NULL
        ORDER BY show_index DESC LIMIT 200
    """).fetchall()
    show_ids = [r["showid"] for r in recent_shows]
    
    pairs = Counter()
    for show_id in show_ids:
        songs_in_show = conn.execute("""
            SELECT DISTINCT sg.slug
            FROM performances p
            JOIN songs sg ON p.songid = sg.songid
            WHERE p.showid = ?
        """, (show_id,)).fetchall()
        slugs = sorted([s["slug"] for s in songs_in_show])
        for i in range(len(slugs)):
            for j in range(i+1, len(slugs)):
                pairs[(slugs[i], slugs[j])] += 1
                
    for (s1, s2), count in pairs.most_common(20):
        print(f"{s1} + {s2} : {count} shows out of 200 ({count/2:.1f}%)")

if __name__ == "__main__":
    main()
