import os
import subprocess
import sys
from pathlib import Path
from phishpred.config import _load_env
from phishpred.db import get_connection
from phishpred.mcp import tools

def main():
    # Load environment variables (such as PHISHNET_API_KEY, R2_* vars)
    _load_env()
    
    # 1. Run database refresh
    print("Running phishpred refresh to fetch any new completed shows from phish.net API...")
    try:
        subprocess.run([sys.executable, "-m", "phishpred.cli", "refresh"], check=True)
    except Exception as e:
        print(f"Error running database refresh: {e}", file=sys.stderr)
        # Continue anyway, in case the DB was already updated by another means
        
    # 2. Get the latest completed showdate from the database
    conn = get_connection("data/phish.db")
    try:
        recent = tools.recent_setlists(conn, n=1)
        shows = recent.get("shows", [])
        if not shows:
            print("No completed shows found in the database. Exiting.")
            sys.exit(0)
        latest_showdate = shows[0]["showdate"]
    except Exception as e:
        print(f"Error querying recent setlists from database: {e}", file=sys.stderr)
        sys.exit(1)
        
    print(f"Latest completed show in DB: {latest_showdate}")
    
    # 3. Read the last seen showdate
    state_file = Path("agents/antigravity/last_seen_showdate.txt")
    last_seen = ""
    if state_file.exists():
        last_seen = state_file.read_text(encoding="utf-8").strip()
        print(f"Last processed showdate: {last_seen}")
    else:
        print("No last seen showdate file found. We will proceed to generate predictions and initialize the state.")
        
    # If the latest showdate is newer than the last seen showdate (or if no last seen showdate)
    if not last_seen or latest_showdate > last_seen:
        print(f"New show completed ({latest_showdate} > {last_seen}). Generating new predictions...")
        
        # 4. Run make_predictions.py
        try:
            print("Executing make_predictions.py...")
            subprocess.run([sys.executable, "agents/antigravity/make_predictions.py"], check=True)
        except Exception as e:
            print(f"Error running make_predictions.py: {e}", file=sys.stderr)
            sys.exit(1)
            
        # 5. Run verify_submissions.py
        try:
            print("Executing verify_submissions.py...")
            res = subprocess.run(
                [sys.executable, "agents/antigravity/verify_submissions.py"],
                capture_output=True,
                text=True,
                check=True
            )
            print(res.stdout)
            if "failed" in res.stdout.lower() and not "0 failed" in res.stdout.lower():
                print("Verification failed! Submissions have errors.", file=sys.stderr)
                sys.exit(1)
        except Exception as e:
            print(f"Error running verify_submissions.py: {e}", file=sys.stderr)
            sys.exit(1)
            
        # 6. Publish to R2
        try:
            print("Publishing to R2...")
            cmd_args = [
                sys.executable,
                "-c",
                "from phishpred.config import _load_env; _load_env(); from scripts.r2_push import main; main(['data/predictions/submitted', 'submitted'])"
            ]
            subprocess.run(cmd_args, check=True)
            print("Published successfully to R2.")
        except Exception as e:
            print(f"Error pushing to R2: {e}", file=sys.stderr)
            sys.exit(1)
            
        # 7. Update the last seen showdate
        try:
            state_file.write_text(latest_showdate, encoding="utf-8")
            print(f"Updated last processed showdate to {latest_showdate}")
        except Exception as e:
            print(f"Warning: Failed to update last seen showdate file: {e}", file=sys.stderr)
            
        print("Run complete.")
    else:
        print(f"No new shows since last run ({latest_showdate} is not newer than {last_seen}). No action needed.")

if __name__ == "__main__":
    main()
