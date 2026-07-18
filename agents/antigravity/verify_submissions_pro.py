import json
from pathlib import Path

def main():
    label = "gemini-3.1-pro-high"
    sub_dir = Path("data/predictions/submitted") / "gemini-3-1-pro-high"
    
    if not sub_dir.exists():
        print(f"Directory {sub_dir} does not exist.")
        return
        
    from phishpred.db import get_connection
    from phishpred.mcp import tools
    conn = get_connection("data/phish.db")
    upcoming_res = tools.upcoming_shows(conn, limit=50)
    upcoming_dates = {s["showdate"] for s in upcoming_res.get("shows", [])}
    
    json_files = sorted(sub_dir.glob("*.json"))
    json_files = [f for f in json_files if f.stem in upcoming_dates]
    print(f"Verifying {len(json_files)} files in {sub_dir}...\n")
    
    failed = 0
    passed = 0
    rationales = set()
    
    for f in json_files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[-] {f.name} is not valid JSON: {e}")
            failed += 1
            continue
            
        showdate = f.stem
        # Check model_label
        if data.get("model_label") != label:
            print(f"[-] {f.name}: model_label is {data.get('model_label')!r}, expected {label!r}")
            failed += 1
            continue
            
        # Check showdate matches
        if data.get("showdate") != showdate:
            print(f"[-] {f.name}: showdate in file is {data.get('showdate')!r}, expected {showdate!r}")
            failed += 1
            continue
            
        # Check rationale
        rat = data.get("rationale")
        if not rat:
            print(f"[-] {f.name}: missing rationale")
            failed += 1
            continue
        if rat in rationales:
            print(f"[-] {f.name}: duplicate rationale (reuse of rationale is not allowed)")
            failed += 1
            continue
        rationales.add(rat)
        
        # Check predictions
        preds = data.get("predictions", [])
        if not isinstance(preds, list) or not (20 <= len(preds) <= 40):
            print(f"[-] {f.name}: predictions list length is {len(preds)}, expected 20 to 40")
            failed += 1
            continue
            
        pred_slugs = set()
        pred_prob_sum = 0.0
        for i, entry in enumerate(preds):
            slug = entry.get("slug")
            prob = entry.get("prob")
            if not slug or prob is None:
                print(f"[-] {f.name}: prediction index {i} is missing slug or prob")
                failed += 1
                break
            if slug in pred_slugs:
                print(f"[-] {f.name}: duplicate slug {slug!r} in predictions")
                failed += 1
                break
            pred_slugs.add(slug)
            pred_prob_sum += prob
            
        # Check setlist
        sl = data.get("setlist")
        if not sl or "sets" not in sl:
            print(f"[-] {f.name}: missing setlist.sets")
            failed += 1
            continue
            
        sets = sl["sets"]
        sl_slugs = set()
        for s_lbl, songs in sets.items():
            if not isinstance(songs, list) or not songs:
                print(f"[-] {f.name}: set {s_lbl!r} is not a non-empty list of songs")
                failed += 1
                break
            for song in songs:
                if song in sl_slugs:
                    print(f"[-] {f.name}: duplicate song {song!r} in setlist")
                    failed += 1
                    break
                sl_slugs.add(song)
                
        print(f"[+] {f.name} PASSED. predictions={len(preds)} (prob_sum={pred_prob_sum:.2f}), setlist_songs={len(sl_slugs)}")
        passed += 1
        
    print(f"\nVerification finished: {passed} passed, {failed} failed.")

if __name__ == "__main__":
    main()
