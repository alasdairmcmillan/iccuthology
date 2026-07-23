import json
from pathlib import Path

sub_dir = Path("data/predictions/submitted/gemini-3-6-flash-high")
assert sub_dir.exists(), f"Directory {sub_dir} does not exist!"

files = sorted(list(sub_dir.glob("*.json")))
print(f"Verifying {len(files)} submission files in {sub_dir}...")

rationales = set()
for f in files:
    data = json.loads(f.read_text(encoding="utf-8"))
    
    # 1. Model label check
    assert data["model_label"] == "gemini-3.6-flash-high", f"Label mismatch in {f.name}: {data.get('model_label')}"
    
    # 2. Setlist sets key check
    assert "setlist" in data and "sets" in data["setlist"], f"Missing setlist.sets in {f.name}"
    
    # 3. Predictions length & range check
    preds = data.get("predictions", [])
    assert 20 <= len(preds) <= 40, f"Predictions count out of range in {f.name}: {len(preds)}"
    for p in preds:
        assert 0 < p["prob"] <= 1.0, f"Prob out of range in {f.name}: {p}"
        
    # 4. Rationale check
    rat = data.get("rationale")
    assert rat and len(rat) > 20, f"Rationale missing or too short in {f.name}"
    assert rat not in rationales, f"Duplicate rationale detected in {f.name}"
    rationales.add(rat)

print("All 11 submission files verified successfully!")
