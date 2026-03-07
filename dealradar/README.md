# DealPulses — DealRadar™ Monitoring Engine

Watches **30+ RSS feeds** across retailers, deal sites, and finance blogs.
Scores every deal 0–100, fires instant email alerts for hot deals, and
sends a daily digest — all automatically.

---

## Quick Start (5 minutes)

### Step 1 — Install dependencies
```bash
pip install -r requirements.txt
```

### Step 2 — Configure your email
Open `dealradar.py` and edit the `CONFIG` block at the top:

```python
"email": {
    "sender_email": "your@gmail.com",       # ← Your Gmail
    "sender_pass":  "xxxx xxxx xxxx xxxx",  # ← Gmail App Password (16 chars)
    "alert_to":     ["your@gmail.com"],      # ← Where HOT alerts go
    "digest_to":    ["your@gmail.com"],      # ← Where daily digest goes
},
```

**How to get a Gmail App Password:**
1. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
2. Sign in → Select app: "Mail" → Select device: "Other" → name it "DealRadar"
3. Copy the 16-character password it gives you → paste into `sender_pass`

### Step 3 — Test your email config
```bash
python dealradar.py --test
```
You should receive a confirmation email within 30 seconds.

### Step 4 — Run your first scan
```bash
python dealradar.py --top 10
```
This prints the top 10 deals found right now to your terminal (no email sent).

### Step 5 — Run live with alerts
```bash
python dealradar.py
```
Scans all feeds, saves deals to the database, and emails you any HOT deals
(score ≥ 75/100) it finds.

---

## All Commands

| Command | What it does |
|---|---|
| `python dealradar.py` | Scan feeds, send hot alerts |
| `python dealradar.py --digest` | Scan feeds + send full daily digest |
| `python dealradar.py --test` | Send a test email only |
| `python dealradar.py --top 10` | Print top 10 deals to terminal |
| `python dealradar.py --top 20 --digest` | Print + send digest |

---

## Schedule It (Run Automatically 24/7)

### On Mac/Linux — add to crontab:
```bash
crontab -e
```
Add these two lines:
```
# Scan every 15 minutes for hot deals
*/15 * * * * cd /path/to/dealradar && python dealradar.py >> dealradar.log 2>&1

# Send daily digest every morning at 8am
0 8 * * * cd /path/to/dealradar && python dealradar.py --digest >> dealradar.log 2>&1
```

### On Windows — use Task Scheduler:
1. Open Task Scheduler → Create Basic Task
2. Trigger: Daily, repeat every 15 minutes
3. Action: Start a program → `python` → Arguments: `C:\path\to\dealradar.py`

### On a server (recommended for 24/7 uptime):
Deploy to a free/cheap cloud server and use cron.
Recommended: [Railway.app](https://railway.app) free tier or
[Render.com](https://render.com) free tier.

---

## How Deal Scoring Works

Every deal gets scored 0–100 based on:

| Factor | Max Points |
|---|---|
| Discount depth (70%+ off = max) | 40 pts |
| Absolute price drop ($200+ = max) | 10 pts |
| Power keywords ("all-time low", "flash sale") | +25 pts |
| Category weight (finance/tech = multiplied) | ×1.5 |
| Source priority (high = trusted sources) | +10 pts |

**Score thresholds:**
- **75–100** → Instant email alert fired immediately
- **40–74** → Included in daily digest
- **0–39** → Saved to database, not emailed

---

## Tuning for Your Audience

Edit the `CONFIG` block in `dealradar.py`:

```python
# Lower this to get more (but noisier) hot alerts
"hot_alert_score": 75,

# Lower this to include more deals in the digest
"digest_min_score": 40,

# Only alert on deals with at least this % discount
"min_discount_pct": 15,

# Boost finance deals even higher (Doctor of Credit competitor)
"category_weights": {
    "finance": 1.5,   # ← Increase to prioritise bank bonuses
    "tech":    1.4,   # ← Increase to prioritise tech deals
}
```

---

## Adding More Feeds

Add any RSS feed to the `FEEDS` list:

```python
{
    "name":     "My Custom Feed",
    "url":      "https://example.com/deals/feed.xml",
    "category": "tech",       # tech, finance, gaming, travel, general
    "priority": "high",       # high, medium, low
},
```

---

## Database

All deals are saved to `dealradar.db` (SQLite).
View it with [DB Browser for SQLite](https://sqlitebrowser.org) (free).

Useful queries:
```sql
-- Top 20 deals ever found
SELECT title, score, price_now, price_was, discount, source
FROM deals ORDER BY score DESC LIMIT 20;

-- All finance/bank bonus deals
SELECT * FROM deals WHERE category = 'finance' ORDER BY first_seen DESC;

-- Deals alerted today
SELECT d.title, a.sent_at FROM alerts_sent a
JOIN deals d ON d.id = a.deal_id
WHERE a.sent_at > date('now') ORDER BY a.sent_at DESC;
```

---

## Files

```
dealradar/
├── dealradar.py        ← Main script (edit CONFIG at the top)
├── requirements.txt    ← Python dependencies
├── README.md           ← This file
├── dealradar.db        ← SQLite database (auto-created on first run)
└── dealradar.log       ← Log file (auto-created on first run)
```

---

Built for **DealPulses** — [dealpulses.com](https://dealpulses.com)
