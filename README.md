
# Expense Calculator Demo (FastAPI)

Event-based expense split with magic-link login (demo), volunteer overpay, underpay bids, and a live pie chart.

## Run on GitHub Codespaces (works on iPad)
1. Create a new repo on GitHub.
2. Upload **all files** from this ZIP (keep folders).
3. Click **Code → Create codespace on main**.
4. When the terminal is ready, run:
   ```bash
   bash scripts/run.sh
   ```
5. Codespaces will forward **port 8000** and open the app.

## Demo flow
- Home → get a demo "magic link" (redirects with `?token=`).
- Create an event → you're auto-added as a participant.
- Invite someone → token is created in DB.
- Simulate joining via `/event/{EVENT}/join/{TOKEN}`.
- Add pledges (overpay or underpay bids) → pie updates.
