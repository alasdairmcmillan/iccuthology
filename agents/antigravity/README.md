# agents/antigravity — Antigravity's workspace

This folder belongs to the Antigravity harness (and the model tracks it
drives). Anything you build to improve your own predictions lives here and
gets committed: analysis scripts, research helpers, calibration notebooks,
prompt notes, scratch data derived from read-only queries. Organize it
however you like — subfolders, a `notes.md`, whatever works.

## Rules (the sandbox contract)

1. **Write only inside this folder.** Never modify `phishpred` source,
   tests, contracts, docs, workflows, or other agents' workspaces. If you
   think core code should change, write up the proposal here (e.g.
   `proposals/…md`) and the human will review it.
2. **The repo may not depend on you.** Nothing under `src/`, `tests/`,
   `worker/`, or `web/` may import or invoke code in this folder. Your
   scripts may freely import `phishpred` the other way.
3. **Read-only against the data.** Open `data/phish.db` read-only; never
   INSERT/UPDATE/DELETE. Prediction submissions still go ONLY through
   `phishpred.mcp.tools.submit_prediction` (see AGENTS.md steps 4–6) — a
   script here may *call* it, but must not write submission files directly.
4. **No secrets.** Never commit API keys, `.env*` contents, or anything
   from `.env.local`. Load env vars at runtime like the repo's own scripts
   do (`phishpred.config._load_env`).
5. **Throwaway stays in `tmp/`.** This folder is for tooling worth keeping;
   one-off dumps and debug output still go to the gitignored `tmp/`.

## Useful entry points

```python
from phishpred.db import get_connection
from phishpred.mcp import tools

conn = get_connection("data/phish.db")
tools.scoreboard("data/scorecards", model_label="<your-label>")  # your record vs baseline
tools.show_length_stats(conn)                                    # songs/show calibration
tools.slot_propensities(conn, slugs)                             # set-position tendencies + era structure
tools.backtest_shortlist(conn, slugs)                            # test a hypothesis on recent shows
tools.candidate_features(conn, showdate)                         # the feature frame
```

Model-label rules, the submission contract, and the publish checklist are in
AGENTS.md and `docs/MCP.md` § "Agent playbook — driving a live model track".
