import os
import json

sub_dir = "data/predictions/submitted/gemini-3-5-flash-high"

target_shows = [
    "2026-07-21.json",
    "2026-07-22.json",
    "2026-07-24.json",
    "2026-07-25.json",
    "2026-07-27.json",
    "2026-07-29.json",
    "2026-07-31.json",
    "2026-08-01.json",
    "2026-09-04.json",
    "2026-09-05.json",
    "2026-09-06.json"
]

def verify():
    print(f"Checking {len(target_shows)} upcoming show files in {sub_dir}:")
    rationales = set()
    for fn in target_shows:
        path = os.path.join(sub_dir, fn)
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        label = d.get("model_label")
        showdate = d.get("showdate")
        setlist = d.get("setlist")
        rationale = d.get("rationale")
        preds = d.get("predictions")
        
        sets_keys = list(setlist.get("sets", {}).keys()) if setlist else None
        print(f"  {fn}: label={label}, showdate={showdate}, preds_count={len(preds)}, setlist_sets={sets_keys}")
        
        assert label == "gemini-3.5-flash-high", f"Bad label: {label}"
        assert setlist and "sets" in setlist, f"Missing setlist: {fn}"
        assert rationale, f"Missing rationale: {fn}"
        assert rationale not in rationales, f"Duplicate rationale found in {fn}"
        rationales.add(rationale)

    print("\nALL 11 UPCOMING SHOW VERIFICATIONS PASSED PERFECTLY!")

if __name__ == "__main__":
    verify()
