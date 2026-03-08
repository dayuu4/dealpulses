#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║          DealPulses — DealRadar™ Monitoring Engine              ║
║  Watches 30+ RSS feeds, scores deals, and fires email alerts    ║
║  Run once manually or schedule via cron for 24/7 monitoring     ║
╚══════════════════════════════════════════════════════════════════╝

Usage:
    python dealradar.py              # Run once, send alerts if hot deals found
    python dealradar.py --digest     # Force-send full daily digest email
    python dealradar.py --test       # Test email config without fetching feeds
    python dealradar.py --top 10     # Print top 10 deals to terminal only (no email)

Schedule (add to crontab with: crontab -e):
    */15 * * * * cd /path/to/dealradar && python dealradar.py          # Every 15 min
    0 8  * * * cd /path/to/dealradar && python dealradar.py --digest   # Daily digest 8am
"""

# ─────────────────────────────────────────────────────────────────
#  CONFIGURATION  ← Edit these settings before running
# ─────────────────────────────────────────────────────────────────
CONFIG = {

    # ── Email Settings ──────────────────────────────────────────
    # Gmail: enable "App Passwords" at myaccount.google.com/apppasswords
    # Then paste the 16-char app password below (NOT your Gmail password)
    "email": {
        "enabled":       True,
        "smtp_host":     "smtp.gmail.com",
        "smtp_port":     587,
        "sender_email":  "dealpulses@gmail.com",          # ← YOUR Gmail address
        "sender_pass":   "down mczf tzua zcft",     		  # ← 16-char App Password
        "alert_to":      ["dealpulses@gmail.com"],        # ← Where to send HOT deal alerts
        "digest_to":     ["dealpulses@gmail.com"],        # ← Where to send daily digests
    },

    # ── Deal Scoring Thresholds ─────────────────────────────────
    "thresholds": {
        "hot_alert_score":    75,    # Score ≥ this → instant email alert
        "digest_min_score":   40,    # Score ≥ this → included in digest
        "min_discount_pct":   15,    # Ignore deals with < 15% discount
        "min_price_drop":     5.00,  # Ignore price drops under $5
    },

    # ── Behaviour ───────────────────────────────────────────────
    "settings": {
        "db_path":            "dealradar.db",   # SQLite database file
        "log_path":           "dealradar.log",  # Log file
        "resend_hours":       24,               # Don't re-alert on same deal for 24h
        "max_deals_digest":   20,               # Max deals in a digest email
        "request_timeout":    5,                # HTTP timeout in seconds
        "user_agent":         "DealPulses/1.0 DealRadar Bot (+https://dealpulses.com)",
        "max_age_days":       3,                # ← Skip any entry older than 3 days
        "resolve_merchant_urls": True,          # ← Follow aggregator links → direct merchant URL
        "parse_workers":         20,            # ← Parallel threads for parse_entry (URL resolution + image fetch)
        "export_json":     True,                # ← Write deals.json + price_history/ after each scan
        "export_json_dir": ".",                 # ← Folder to write to (set to your site root)
        "affiliate_tags": {
            "amazon.com":   "tag=dealpulses0e-20",   # ← Amazon Associates tag
            # "walmart.com": "wmlspartner=XXXXX",    # add others here when approved
        },
    },

    # ── Category Weights (higher = more likely to alert) ────────
    "category_weights": {
        "tech":      1.4,
        "laptop":    1.4,
        "phone":     1.3,
        "gaming":    1.3,
        "audio":     1.2,
        "tv":        1.2,
        "software":  1.2,
        "finance":   1.5,   # Bank bonuses = high priority
        "travel":    1.1,
        "general":   1.0,
    },
}

# ─────────────────────────────────────────────────────────────────
#  RSS FEED SOURCES
#  Organised into 6 groups:
#    1. Trusted deal aggregators       (broad, high-quality curation)
#    2. Store-specific Slickdeals RSS  (one feed per major retailer)
#    3. Direct retailer deal feeds     (official or semi-official)
#    4. Tech & gadget deal publishers  (deals-ONLY sites/sections)
#    5. Gaming deals                   (consoles, games, peripherals)
#    6. Finance & bank bonuses         (Doctor of Credit competitors)
#
#  Every entry goes through a max_age_days freshness filter in
#  parse_entry(), so stale articles are dropped automatically.
# ─────────────────────────────────────────────────────────────────
FEEDS = [

    # ════════════════════════════════════════════════════════════
    #  GROUP 1 — TRUSTED DEAL AGGREGATORS
    #  Pure deal curation, no general news mixed in
    # ════════════════════════════════════════════════════════════
    {
        "name":     "Slickdeals Front Page",
        "url":      "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1",
        "category": "general",
        "priority": "high",
    },
    {
        "name":     "Slickdeals Popular Deals",
        "url":      "https://slickdeals.net/newsearch.php?mode=popdeals&searcharea=deals&searchin=first&rss=1",
        "category": "general",
        "priority": "high",
    },
    {
        "name":     "DealNews Top Deals",
        "url":      "https://dealnews.com/feeds/popular.rss",
        "category": "general",
        "priority": "high",
    },
    {
        "name":     "DealNews Electronics",
        "url":      "https://dealnews.com/c196/Electronics/feeds/popular.rss",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "DealNews Computers",
        "url":      "https://dealnews.com/c6/Computers/feeds/popular.rss",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "DealNews Mobile Phones",
        "url":      "https://dealnews.com/c357/Cell-Phones-Plans/feeds/popular.rss",
        "category": "phone",
        "priority": "high",
    },
    {
        "name":     "DealNews Gaming",
        "url":      "https://dealnews.com/c217/Gaming/feeds/popular.rss",
        "category": "gaming",
        "priority": "high",
    },
    # Ben's Bargains, Brad's Deals, Wirecutter removed — their links
    # resolve through multi-hop redirects that end at google.com, not merchants.
    {
        "name":     "Woot! Daily Deals",
        "url":      "https://www.woot.com/blog/feed.xml",
        "category": "tech",
        "priority": "high",
    },
    # TechBargains removed — resolves through Google redirect chains.
    {
        "name":     "FatWallet Tech",
        "url":      "https://slickdeals.net/newsearch.php?q=deal+%25+off&searcharea=deals&searchin=first&rss=1",
        "category": "general",
        "priority": "medium",
    },

    # ════════════════════════════════════════════════════════════
    #  GROUP 2 — STORE-SPECIFIC SLICKDEALS RSS
    #  Filters Slickdeals community deals by retailer name.
    #  These are the most reliable real-time source per store.
    # ════════════════════════════════════════════════════════════
    {
        "name":     "Amazon Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=amazon&searcharea=deals&searchin=first&rss=1",
        "category": "general",
        "priority": "high",
    },
    {
        "name":     "Walmart Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=walmart&searcharea=deals&searchin=first&rss=1",
        "category": "general",
        "priority": "high",
    },
    {
        "name":     "Best Buy Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=best+buy&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "Target Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=target&searcharea=deals&searchin=first&rss=1",
        "category": "general",
        "priority": "high",
    },
    {
        "name":     "Newegg Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=newegg&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "Micro Center Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=micro+center&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "Costco Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=costco&searcharea=deals&searchin=first&rss=1",
        "category": "general",
        "priority": "high",
    },
    {
        "name":     "B&H Photo Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=b%26h&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "Adorama Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=adorama&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "medium",
    },
    {
        "name":     "Dell Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=dell&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "Lenovo Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=lenovo&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "HP Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=hp+deal&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "medium",
    },
    {
        "name":     "Samsung Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=samsung&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "Apple Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=apple&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "Microsoft Store Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=microsoft+store&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "medium",
    },
    {
        "name":     "Anker Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=anker&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "Logitech Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=logitech&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "medium",
    },
    {
        "name":     "GameStop Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=gamestop&searcharea=deals&searchin=first&rss=1",
        "category": "gaming",
        "priority": "medium",
    },
    {
        "name":     "Razer Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=razer&searcharea=deals&searchin=first&rss=1",
        "category": "gaming",
        "priority": "medium",
    },
    {
        "name":     "Corsair Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=corsair&searcharea=deals&searchin=first&rss=1",
        "category": "gaming",
        "priority": "medium",
    },
    {
        "name":     "LG Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=lg+deal&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "medium",
    },
    {
        "name":     "Sony Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=sony&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "TCL / Hisense TV Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=tcl+hisense+tv&searcharea=deals&searchin=first&rss=1",
        "category": "tv",
        "priority": "medium",
    },
    {
        "name":     "Sam's Club Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=sams+club&searcharea=deals&searchin=first&rss=1",
        "category": "general",
        "priority": "medium",
    },
    {
        "name":     "Monoprice Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=monoprice&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "medium",
    },
    {
        "name":     "Staples Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=staples&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "low",
    },
    {
        "name":     "eBay Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=ebay&searcharea=deals&searchin=first&rss=1",
        "category": "general",
        "priority": "medium",
    },
    {
        "name":     "Overstock Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=overstock&searcharea=deals&searchin=first&rss=1",
        "category": "general",
        "priority": "low",
    },

    # ════════════════════════════════════════════════════════════
    #  GROUP 3 — DIRECT RETAILER FEEDS
    #  Official/direct deal pages with native RSS support
    # ════════════════════════════════════════════════════════════
    {
        "name":     "Woot! Electronics",
        "url":      "https://electronics.woot.com/feeds/all.rss",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "Woot! Computers",
        "url":      "https://computers.woot.com/feeds/all.rss",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "CamelCamelCamel — Amazon Price Drops",
        "url":      "https://camelcamelcamel.com/top_drops/feed",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "CamelCamelCamel — Popular Electronics",
        "url":      "https://camelcamelcamel.com/popular/rss?deal=1&catid=172282",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "eBay Daily Deals",
        "url":      "https://deals.ebay.com/deals/rss",
        "category": "general",
        "priority": "medium",
    },

    # ════════════════════════════════════════════════════════════
    #  GROUP 4 — removed: editorial sites (9to5Toys, 9to5Mac,
    #  Android Authority, The Verge, Tom's Hardware, Tom's Guide,
    #  PCMag, CNET, Digital Trends, Laptop Mag) all route their
    #  article links through Google AMP/redirect chains that end
    #  at google.com/preferences — not real merchant product pages.
    # ════════════════════════════════════════════════════════════

    # ════════════════════════════════════════════════════════════
    #  GROUP 5 — GAMING DEALS
    # ════════════════════════════════════════════════════════════
    # IGN Game Deals + IsThereAnyDeal removed — editorial/tracker sites
    # that don't resolve to direct merchant product pages.
    # Humble Bundle kept as future scraping candidate when RSS is added.
    {
        "name":     "Slickdeals PS5",
        "url":      "https://slickdeals.net/newsearch.php?q=ps5&searcharea=deals&searchin=first&rss=1",
        "category": "gaming",
        "priority": "medium",
    },
    {
        "name":     "Slickdeals Xbox",
        "url":      "https://slickdeals.net/newsearch.php?q=xbox&searcharea=deals&searchin=first&rss=1",
        "category": "gaming",
        "priority": "medium",
    },
    {
        "name":     "Slickdeals Nintendo",
        "url":      "https://slickdeals.net/newsearch.php?q=nintendo&searcharea=deals&searchin=first&rss=1",
        "category": "gaming",
        "priority": "medium",
    },

    # ════════════════════════════════════════════════════════════
    #  GROUP 7 — YOUR TOP RETAILERS (SLICKDEALS RSS)
    #  Covers every store on the DealPulses priority retailer list
    #  that was not already in Group 2.
    # ════════════════════════════════════════════════════════════
    {
        "name":     "AliExpress Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=aliexpress&searcharea=deals&searchin=first&rss=1",
        "category": "general",
        "priority": "high",
    },
    {
        "name":     "Woot! Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=woot&searcharea=deals&searchin=first&rss=1",
        "category": "general",
        "priority": "high",
    },
    {
        "name":     "Woot! Sport & Outdoors",
        "url":      "https://sport.woot.com/feeds/all.rss",
        "category": "general",
        "priority": "medium",
    },
    {
        "name":     "Woot! Tools & Garden",
        "url":      "https://tools.woot.com/feeds/all.rss",
        "category": "general",
        "priority": "medium",
    },
    {
        "name":     "Woot! Sellout (Daily Deal)",
        "url":      "https://sellout.woot.com/feeds/all.rss",
        "category": "general",
        "priority": "high",
    },
    {
        "name":     "Verizon Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=verizon&searcharea=deals&searchin=first&rss=1",
        "category": "phone",
        "priority": "high",
    },
    {
        "name":     "Google Fi Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=google+fi&searcharea=deals&searchin=first&rss=1",
        "category": "phone",
        "priority": "medium",
    },
    {
        "name":     "Mint Mobile Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=mint+mobile&searcharea=deals&searchin=first&rss=1",
        "category": "phone",
        "priority": "high",
    },
    {
        "name":     "Spectrum Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=spectrum+internet&searcharea=deals&searchin=first&rss=1",
        "category": "general",
        "priority": "medium",
    },
    {
        "name":     "SideDeal Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=sidedeal&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "Google Play Store Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=google+play&searcharea=deals&searchin=first&rss=1",
        "category": "software",
        "priority": "medium",
    },
    {
        "name":     "Greentoe Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=greentoe&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "medium",
    },
    {
        "name":     "Secondipity Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=secondipity&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "medium",
    },

    # ════════════════════════════════════════════════════════════
    #  GROUP 8 — YOUR TOP 50 TECH BRANDS (SLICKDEALS RSS)
    #  One feed per brand — catches deals across ALL retailers
    #  that carry that brand, not just the brand's own store.
    # ════════════════════════════════════════════════════════════
    {
        "name":     "Roku Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=roku&searcharea=deals&searchin=first&rss=1",
        "category": "tv",
        "priority": "high",
    },
    {
        "name":     "Baseus Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=baseus&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "LISEN Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=lisen&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "medium",
    },
    {
        "name":     "ECO-WORTHY Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=eco-worthy&searcharea=deals&searchin=first&rss=1",
        "category": "general",
        "priority": "medium",
    },
    {
        "name":     "Hisense TV Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=hisense&searcharea=deals&searchin=first&rss=1",
        "category": "tv",
        "priority": "high",
    },
    {
        "name":     "Sennheiser Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=sennheiser&searcharea=deals&searchin=first&rss=1",
        "category": "audio",
        "priority": "high",
    },
    {
        "name":     "Bose Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=bose&searcharea=deals&searchin=first&rss=1",
        "category": "audio",
        "priority": "high",
    },
    {
        "name":     "Klipsch Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=klipsch&searcharea=deals&searchin=first&rss=1",
        "category": "audio",
        "priority": "high",
    },
    {
        "name":     "JBL Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=jbl&searcharea=deals&searchin=first&rss=1",
        "category": "audio",
        "priority": "high",
    },
    {
        "name":     "Acer Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=acer&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "ASUS Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=asus&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "TCL Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=tcl&searcharea=deals&searchin=first&rss=1",
        "category": "tv",
        "priority": "high",
    },
    {
        "name":     "RayNeo Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=rayneo&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "medium",
    },
    {
        "name":     "BLUETTI Power Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=bluetti&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "eufy Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=eufy&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "UGREEN Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=ugreen&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "Google Pixel & Fitbit Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=google+pixel&searcharea=deals&searchin=first&rss=1",
        "category": "phone",
        "priority": "high",
    },
    {
        "name":     "Energizer Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=energizer&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "medium",
    },
    {
        "name":     "Cable Matters Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=cable+matters&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "medium",
    },
    {
        "name":     "WAVLINK Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=wavlink&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "medium",
    },
    {
        "name":     "GameSir Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=gamesir&searcharea=deals&searchin=first&rss=1",
        "category": "gaming",
        "priority": "medium",
    },
    {
        "name":     "Fosi Audio Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=fosi+audio&searcharea=deals&searchin=first&rss=1",
        "category": "audio",
        "priority": "medium",
    },
    {
        "name":     "Orico Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=orico&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "medium",
    },
    {
        "name":     "WOLFBOX Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=wolfbox&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "medium",
    },
    {
        "name":     "Westinghouse Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=westinghouse&searcharea=deals&searchin=first&rss=1",
        "category": "tv",
        "priority": "medium",
    },
    {
        "name":     "Mangmi Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=mangmi&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "medium",
    },

    # ════════════════════════════════════════════════════════════
    #  GROUP 9 — REMAINING TOP-100 COMPANIES
    #  Completes full coverage of the Slickdeals top-100 list.
    #  Organised by tier: retail → telecom → streaming → software
    #  → food/travel → home/tools → beauty/apparel → other.
    # ════════════════════════════════════════════════════════════

    # ── Telecom / Carriers ─────────────────────────────────────
    {
        "name":     "T-Mobile Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=t-mobile&searcharea=deals&searchin=first&rss=1",
        "category": "phone",
        "priority": "high",
    },
    {
        "name":     "AT&T Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=att&searcharea=deals&searchin=first&rss=1",
        "category": "phone",
        "priority": "high",
    },
    {
        "name":     "Xfinity Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=xfinity&searcharea=deals&searchin=first&rss=1",
        "category": "phone",
        "priority": "medium",
    },

    # ── Software / SaaS ────────────────────────────────────────
    {
        "name":     "Adobe Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=adobe&searcharea=deals&searchin=first&rss=1",
        "category": "software",
        "priority": "high",
    },
    {
        "name":     "NordVPN Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=nordvpn&searcharea=deals&searchin=first&rss=1",
        "category": "software",
        "priority": "high",
    },
    # ── Cameras / Drones / Action Cams ─────────────────────────
    {
        "name":     "Canon Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=canon&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "DJI Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=dji&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "GoPro Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=gopro&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },

    # ── MSI / Yamaha ────────────────────────────────────────────
    {
        "name":     "MSI Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=msi&searcharea=deals&searchin=first&rss=1",
        "category": "tech",
        "priority": "high",
    },
    {
        "name":     "Yamaha Deals (Slickdeals)",
        "url":      "https://slickdeals.net/newsearch.php?q=yamaha&searcharea=deals&searchin=first&rss=1",
        "category": "audio",
        "priority": "high",
    },

]

# Keywords that boost a deal's score when found in title/description
BOOST_KEYWORDS = {
    "all-time low":     25,
    "all time low":     25,
    "lowest price":     20,
    "price drop":       15,
    "flash sale":       18,
    "lightning deal":   18,
    "limited time":     12,
    "today only":       15,
    "ends tonight":     14,
    "back in stock":    10,
    "exclusive":        8,
    "hot deal":         12,
    "rare deal":        15,
    "expired soon":     10,
    "free shipping":    5,
    "bank bonus":       20,
    "checking bonus":   22,
    "savings bonus":    20,
    "credit card":      15,
    "sign up bonus":    18,
    "new cardmember":   15,
}

# Keywords that reduce a deal's score
PENALTY_KEYWORDS = {
    "ad":           -30,
    "sponsored":    -30,
    "affiliate":    -10,
    "review":       -15,
    "guide":        -10,
    "how to":       -10,
    "best of":      -5,
}

# ─────────────────────────────────────────────────────────────────
#  MERCHANT URL RESOLVER
#  Automatically unwraps aggregator / middleman links so every deal
#  stored in the DB points directly at the retailer, not Slickdeals
#  or any other deal-curation site.
# ─────────────────────────────────────────────────────────────────

# Sites that curate deals but are NOT the actual place to buy
AGGREGATOR_DOMAINS = {
    "slickdeals.net",
    "dealnews.com",
    "dealmoon.com",
    "dealsplus.com",
    "bradsdeals.com",
    "hipsave.com",
    "9to5toys.com",
    "9to5mac.com",
    "theverge.com",
    "tomsguide.com",
    "tomshardware.com",
    "pcmag.com",
    "cnet.com",
    "digitaltrends.com",
    "laptopmag.com",
    "androidauthority.com",
    "phonearena.com",
    "gsmarena.com",
    "ign.com",
    "camelcamelcamel.com",
    "doctorofcredit.com",
    "thepointsguy.com",
    "nerdwallet.com",
    "bankrate.com",
    "creditcards.com",
    "secretflying.com",
    "going.com",
    "isthereanydeal.com",
    "cheapshark.com",
    "fanatical.com",
    # Google domains — editorial feeds often redirect through Google AMP/tracking
    # and end up at google.com/preferences or similar non-product pages.
    "google.com",
    "googleapis.com",
    "amp.google.com",
    # Other editorial/deal-tracker sites whose links never resolve to merchants
    "frequentmiler.com",
    "onemileatatime.com",
    "milevalue.com",
    "bensbargains.net",
    "bradsdeals.com",
    "techbargains.com",
    "wirecutter.com",
    "nytimes.com",
    "macrumors.com",
}

# Direct retailers — a link landing here is already a merchant URL
MERCHANT_DOMAINS = {
    # ── Core marketplaces & big-box ────────────────────────────────
    "amazon.com", "bestbuy.com", "walmart.com", "target.com",
    "costco.com", "samsclub.com", "kohls.com", "macys.com",
    "aliexpress.com", "ae.com",
    # ── Electronics / computers ────────────────────────────────────
    "newegg.com", "bhphotovideo.com", "adorama.com",
    "microcenter.com", "dell.com", "hp.com", "lenovo.com",
    "antonline.com", "tigerdirect.com", "officedepot.com", "staples.com",
    "greentoe.com", "secondipity.com",
    # ── Outlet / flash deal retailers ──────────────────────────────
    "woot.com", "sidedeal.com", "ebay.com", "rakuten.com", "overstock.com",
    # ── Telecom / mobile carriers ──────────────────────────────────
    "verizon.com", "att.com", "tmobile.com",
    "fi.google.com", "mintmobile.com", "spectrum.com",
    "xfinity.com", "cricketwireless.com", "boostmobile.com",
    # ── Shopping / QVC / pharmacy ──────────────────────────────────
    "qvc.com", "hsn.com", "walgreens.com", "cvs.com",
    # ── Google hardware store (Pixel, Chromecast, etc.) ────────────
    # NOTE: play.google.com intentionally excluded — it's the Android
    # app store and Slickdeals posts often redirect to the Slickdeals
    # app listing there, not to any real product deal.
    "store.google.com",
    # ── Home improvement ───────────────────────────────────────────
    "homedepot.com", "lowes.com", "wayfair.com",
    # ── Gaming platforms ───────────────────────────────────────────
    "gamestop.com", "playstation.com", "xbox.com", "nintendo.com",
    "steampowered.com", "humblebundle.com",
    # ── Apparel / sporting goods ───────────────────────────────────
    "underarmour.com", "nike.com", "adidas.com", "dickssportinggoods.com",
    # ── Streaming ──────────────────────────────────────────────────
    "hulu.com", "disneyplus.com", "max.com", "netflix.com",
    "paramountplus.com", "peacocktv.com", "espnplus.com",
    # ── Banks / finance ────────────────────────────────────────────
    "chase.com", "account.chase.com",
    "citi.com", "citibank.com",
    "wellsfargo.com", "sofi.com",
    "bankofamerica.com", "usbank.com",
    "discover.com", "capitalone.com",
    "barclaysus.com", "americanexpress.com", "amex.com",
    "synchrony.com", "ally.com",
    # ── Tech brands — direct storefronts ───────────────────────────
    # User's top-50 brand list
    "apple.com",
    "samsung.com",
    "roku.com",
    "baseus.com",
    "lisen.com",                    # LISEN
    "eco-worthy.com", "ecoworthy.com",
    "anker.com", "ankersound.com",
    "lg.com",
    "hisense.com", "hisenseusa.com",
    "sony.com",
    "sennheiser.com",
    "bose.com",
    "klipsch.com",
    "jbl.com",
    "acer.com",
    "asus.com", "rog.asus.com",
    "tcl.com",
    "rayneo.com",
    "bluettipower.com",             # BLUETTI
    "eufylife.com", "eufy.com",     # eufy
    "ugreen.com",
    "google.com", "store.google.com", "fi.google.com",
    "energizer.com",
    "cablematters.com",
    "wavlink.com",
    "gamesir.com",
    "fosiaudio.com",                # Fosi Audio
    "orico.com",
    "wolfboxtech.com",              # WOLFBOX
    "westinghouse.com", "westinghouseelectronics.com",
    "mangmi.com",
    # ── Tier 3-4 tech brands (newly added) ────────────────────────
    "msi.com",                              # MSI
    "us.yamaha.com", "yamaha.com",          # Yamaha
    "canon.com", "usa.canon.com",           # Canon
    "dji.com", "store.dji.com",             # DJI
    "gopro.com",                            # GoPro
    "dyson.com",                            # Dyson
    "sharkninja.com", "sharkclean.com",     # Shark
    "kitchenaid.com",                       # KitchenAid
    "dewalt.com",                           # DEWALT
    "ridgid.com",                           # RIDGID
    "ryobitools.com",                       # RYOBI
    # ── Apparel / lifestyle ────────────────────────────────────────
    "oldnavy.com", "gap.com", "bananarepublic.com",
    "sephora.com",                          # Sephora
    "fanatics.com",                         # Fanatics
    # ── Food delivery / gig ───────────────────────────────────────
    "doordash.com", "instacart.com",
    "ubereats.com", "grubhub.com",
    # ── Travel & hotels ───────────────────────────────────────────
    "expedia.com", "priceline.com",
    "hotels.com", "booking.com",
    "valvoline.com",
    # ── Software / SaaS ───────────────────────────────────────────
    "adobe.com",
    "turbotax.com", "turbotax.intuit.com",  # TurboTax
    "hrblock.com",                          # H&R Block
    "nordvpn.com",                          # NordVPN
    # ── Streaming (additional) ────────────────────────────────────
    "peacocktv.com",                        # already in set but kept for clarity
    # ── Marketplace / deals ───────────────────────────────────────
    "temu.com",                             # Temu
    "chewy.com",                            # Chewy
    "groupon.com",                          # Groupon
    # ── Pharmacy / warehouse ──────────────────────────────────────
    "officedepot.com", "officemax.com",
}


def _host(url: str) -> str:
    """Return the bare hostname (no www.) of a URL."""
    try:
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""


def inject_affiliate_tag(url: str) -> str:
    """
    Append the correct affiliate tag to a URL if the merchant is in
    CONFIG affiliate_tags and the tag isn't already present.

    Examples:
      amazon.com/dp/B0ABC  →  amazon.com/dp/B0ABC?tag=dealpulses0e-20
      amazon.com/s?k=foo   →  amazon.com/s?k=foo&tag=dealpulses0e-20
    """
    if not url:
        return url
    tags = CONFIG["settings"].get("affiliate_tags", {})
    host = _host(url)
    for domain, param in tags.items():
        if host == domain or host.endswith("." + domain):
            if param.split("=")[0] not in url:   # don't double-add
                sep = "&" if "?" in url else "?"
                return f"{url}{sep}{param}"
    return url


def _is_aggregator(url: str) -> bool:
    """True if the URL is from a known deal-curation middleman."""
    h = _host(url)
    return any(h == d or h.endswith("." + d) for d in AGGREGATOR_DOMAINS)


def _is_merchant(url: str) -> bool:
    """True if the URL lands directly on a retailer / bank."""
    h = _host(url)
    return any(h == d or h.endswith("." + d) for d in MERCHANT_DOMAINS)


def _first_merchant_link(html: str) -> str:
    """
    Parse an HTML blob (RSS description or fetched page) and return
    the first <a href> that points to a known merchant domain.
    """
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all("a", href=True):
            href = tag["href"].strip()
            if href.startswith("http") and _is_merchant(href):
                return href
    except Exception:
        pass
    return ""


def resolve_merchant_url(url: str, entry=None) -> str:
    """
    Given a URL (possibly from an aggregator), return the direct merchant URL.
    Checks the url_cache table first — cache hits skip all HTTP round-trips.
    Falls back to the original URL if resolution fails so the pipeline never breaks.

    Resolution order:
      0. url_cache hit                  → return cached result immediately
      1. Already a merchant URL         → return as-is
      2. Merchant link in RSS entry     → extract from description/content
      3. HTTP HEAD redirect chain       → check final URL after 301/302
      4. HTTP GET + page scan           → parse page for deal button / merchant <a>
      5. Give up                        → return original URL
    """
    # ── 0. Cache lookup ────────────────────────────────────────────
    cached = get_cached_url(url)
    if cached:
        log.debug(f"    ↳ Cache hit: {cached}")
        return cached

    result = _resolve_merchant_url_uncached(url, entry)
    set_cached_url(url, result)
    return result


def _resolve_merchant_url_uncached(url: str, entry=None) -> str:
    """Internal: resolve without cache. Called only by resolve_merchant_url()."""
    if not url:
        return url

    timeout = CONFIG["settings"]["request_timeout"]
    headers = {"User-Agent": CONFIG["settings"]["user_agent"]}

    # ── 1. Already merchant ────────────────────────────────────────
    if _is_merchant(url):
        return inject_affiliate_tag(url)

    # ── 2. Merchant link inside the RSS entry itself ───────────────
    if entry is not None:
        for attr in ("summary", "description"):
            blob = getattr(entry, attr, "") or ""
            found = _first_merchant_link(blob)
            if found:
                log.debug(f"    ↳ Resolved from RSS description: {found}")
                return inject_affiliate_tag(found)
        # feedparser puts full-content under entry.content (a list of dicts)
        content_list = getattr(entry, "content", []) or []
        for item in content_list:
            blob = item.get("value", "") if isinstance(item, dict) else ""
            found = _first_merchant_link(blob)
            if found:
                log.debug(f"    ↳ Resolved from RSS content block: {found}")
                return inject_affiliate_tag(found)

    # ── 3. Follow HTTP redirects (HEAD first — faster, no body) ───
    try:
        resp = requests.head(
            url, headers=headers, timeout=timeout,
            allow_redirects=True, stream=False,
        )
        if _is_merchant(resp.url):
            log.debug(f"    ↳ Resolved via redirect: {resp.url}")
            return inject_affiliate_tag(resp.url)
    except Exception:
        pass

    # ── 4. Fetch the page and scan its content ─────────────────────
    try:
        resp = requests.get(
            url, headers=headers, timeout=timeout, allow_redirects=True,
        )
        # Check final URL after GET redirects (some sites need GET to redirect)
        if _is_merchant(resp.url):
            log.debug(f"    ↳ Resolved via GET redirect: {resp.url}")
            return inject_affiliate_tag(resp.url)

        soup = BeautifulSoup(resp.text, "html.parser")

        # Slickdeals — look for the primary "Go to Deal" / buy button
        slickdeals_selectors = [
            {"class": "dealDetailAmazonLogo"},
            {"class": "dealButton"},
            {"class": "dealDetailButton"},
            {"class": "buyButton"},
            {"data-type": "merchant-link"},
            {"rel": "nofollow"},               # deal links on SD are nofollow
        ]
        for sel in slickdeals_selectors:
            tag = soup.find("a", sel)
            if tag and tag.get("href", "").startswith("http"):
                href = tag["href"]
                if _is_merchant(href):
                    log.debug(f"    ↳ Resolved from deal-page button: {href}")
                    return inject_affiliate_tag(href)

        # Generic fallback — first merchant link anywhere on the page
        found = _first_merchant_link(resp.text)
        if found:
            log.debug(f"    ↳ Resolved from full page scan: {found}")
            return inject_affiliate_tag(found)

    except Exception as e:
        log.debug(f"    ↳ Page fetch failed ({url}): {e}")

    # ── 5. Give up — never break the pipeline ─────────────────────
    log.debug(f"    ↳ Could not resolve, keeping original: {url}")
    return inject_affiliate_tag(url)


# ─────────────────────────────────────────────────────────────────
#  PRODUCT IMAGE EXTRACTOR
#  Fetches the real listing image for any merchant URL so every deal
#  stored in the DB has a hotlinkable photo, not a placeholder.
#
#  Extraction order (most reliable → least):
#    1. Amazon  — data-a-dynamic-image / #landingImage (hi-res ASIN image)
#    2. og:image meta tag            (works on 95%+ of e-commerce sites)
#    3. twitter:image meta tag       (fallback social card image)
#    4. First <img> with width ≥ 200 (last-resort scan)
# ─────────────────────────────────────────────────────────────────

def fetch_product_image(url: str) -> str:
    """
    Given a direct merchant URL, return a hotlinkable product image URL.
    Returns empty string if nothing is found — never breaks the pipeline.
    """
    if not url:
        return ""

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    timeout = CONFIG["settings"]["request_timeout"]

    try:
        resp = requests.get(url, headers=headers, timeout=timeout,
                            allow_redirects=True)
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        host = _host(resp.url)

        # ── 1. Amazon — extract hi-res image from ASIN product page ───
        if "amazon.com" in host:
            tag = soup.find(id="landingImage") or soup.find(id="imgBlkFront")
            if tag:
                dyn = tag.get("data-a-dynamic-image", "")
                if dyn:
                    import json as _json
                    try:
                        imgs = _json.loads(dyn)
                        # Pick the largest image (highest pixel width)
                        best_url = max(imgs.items(), key=lambda kv: kv[1][0])[0]
                        log.debug(f"    🖼  Amazon hi-res: {best_url[:70]}")
                        return best_url
                    except Exception:
                        pass
                src = tag.get("src", "")
                if src and src.startswith("http"):
                    # Strip thumbnail size tokens to get full-res
                    src = re.sub(r'\._[A-Z0-9_,]+_\.', '.', src)
                    log.debug(f"    🖼  Amazon src: {src[:70]}")
                    return src

        # ── 2. og:image — universal (Best Buy, eBay, Walmart, Target…) ─
        og = (soup.find("meta", property="og:image") or
              soup.find("meta", attrs={"name": "og:image"}))
        if og:
            content = og.get("content", "").strip()
            if content.startswith("http"):
                log.debug(f"    🖼  og:image: {content[:70]}")
                return content

        # ── 3. twitter:image fallback ──────────────────────────────────
        tw = (soup.find("meta", attrs={"name": "twitter:image"}) or
              soup.find("meta", property="twitter:image"))
        if tw:
            content = tw.get("content", "").strip()
            if content.startswith("http"):
                log.debug(f"    🖼  twitter:image: {content[:70]}")
                return content

        # ── 4. First large <img> on the page ──────────────────────────
        for img in soup.find_all("img", src=True):
            src = img.get("src", "")
            if not src.startswith("http"):
                continue
            try:
                if int(str(img.get("width", "0")).replace("px", "")) >= 200:
                    log.debug(f"    🖼  large img fallback: {src[:70]}")
                    return src
            except (ValueError, TypeError):
                pass

    except Exception as e:
        log.debug(f"    🖼  Image fetch failed for {url}: {e}")

    return ""


# ─────────────────────────────────────────────────────────────────
#  IMPORTS & SETUP
# ─────────────────────────────────────────────────────────────────
import re
import sys
import json
import time
import logging
import sqlite3
import smtplib
import hashlib
import argparse
import datetime
from urllib.parse import urlparse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import feedparser
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Run: pip install -r requirements.txt")
    sys.exit(1)

# ── Allow CI/GitHub Actions to inject the SMTP password via env var ──
# Set it as a repo secret: Settings → Secrets & Variables → Actions
# Secret name: DEALRADAR_SMTP_PASS
import os as _os
_smtp_env = _os.environ.get("DEALRADAR_SMTP_PASS")
if _smtp_env:
    CONFIG["email"]["sender_pass"] = _smtp_env

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["settings"]["log_path"]),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("DealRadar")


# ─────────────────────────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────────────────────────
def init_db():
    """Create SQLite tables if they don't exist."""
    con = sqlite3.connect(CONFIG["settings"]["db_path"])
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS deals (
            id          TEXT PRIMARY KEY,
            title       TEXT,
            url         TEXT,
            source      TEXT,
            category    TEXT,
            score       INTEGER,
            price_now   REAL,
            price_was   REAL,
            discount    REAL,
            summary     TEXT,
            image_url   TEXT DEFAULT '',
            alerted     INTEGER DEFAULT 0,
            first_seen  TEXT,
            last_seen   TEXT
        )
    """)

    # Migrate existing databases — add new columns if they don't exist yet
    for migration in [
        "ALTER TABLE deals ADD COLUMN image_url  TEXT DEFAULT ''",
        "ALTER TABLE deals ADD COLUMN status     TEXT DEFAULT 'active'",
        "ALTER TABLE deals ADD COLUMN checked_at TEXT DEFAULT ''",
    ]:
        try:
            cur.execute(migration)
            con.commit()
        except Exception:
            pass  # Column already exists — safe to ignore

    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts_sent (
            deal_id     TEXT,
            sent_at     TEXT,
            alert_type  TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            deal_id     TEXT,
            price       REAL,
            date        TEXT,
            PRIMARY KEY (deal_id, date)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS url_cache (
            original_url  TEXT PRIMARY KEY,
            resolved_url  TEXT,
            cached_at     TEXT
        )
    """)

    con.commit()

    # ── Startup cleanup: purge any deals whose stored URL is not a real merchant
    # This runs every startup and is idempotent — once clean the DELETE is a no-op.
    _JUNK_PATTERNS = [
        # App store redirects (Slickdeals app links)
        "%play.google.com%",
        "%apps.apple.com%",
        # Raw aggregator links that were stored before resolver was working
        "%slickdeals.net%",
        "%dealnews.com%",
        # Google redirect/AMP chains from editorial feeds
        "%google.com%",
        # Editorial / deal-tracker sites that are not merchants
        "%frequentmiler.com%",
        "%onemileatatime.com%",
        "%thepointsguy.com%",
        "%camelcamelcamel.com%",
        "%androidauthority.com%",
        "%9to5toys.com%",
        "%9to5mac.com%",
        "%theverge.com%",
        "%tomshardware.com%",
        "%tomsguide.com%",
        "%pcmag.com%",
        "%cnet.com%",
        "%digitaltrends.com%",
        "%laptopmag.com%",
        "%ign.com%",
        "%bensbargains.net%",
        "%bradsdeals.com%",
        "%techbargains.com%",
        "%isthereanydeal.com%",
        "%cheapshark.com%",
        "%nytimes.com%",
        "%wirecutter.com%",
        "%macrumors.com%",
        "%doctorofcredit.com%",
        "%nerdwallet.com%",
        "%milevalue.com%",
        "%bankrate.com%",
        "%creditcards.com%",
        "%secretflying.com%",
    ]
    has_cache = bool(cur.execute(
        "SELECT name FROM sqlite_master WHERE name='url_cache'"
    ).fetchone())
    for pat in _JUNK_PATTERNS:
        deleted = cur.execute(
            "DELETE FROM deals WHERE url LIKE ?", (pat,)
        ).rowcount
        if deleted:
            log.info(f"DB cleanup: removed {deleted} junk deals matching {pat!r}")
        if has_cache:
            deleted_c = cur.execute(
                "DELETE FROM url_cache WHERE resolved_url LIKE ?", (pat,)
            ).rowcount
            if deleted_c:
                log.info(f"DB cleanup: cleared {deleted_c} url_cache entries matching {pat!r}")

    con.commit()
    con.close()


def get_db():
    return sqlite3.connect(CONFIG["settings"]["db_path"])


def deal_id(title, url):
    """Deterministic ID for deduplication."""
    raw = f"{title.lower().strip()}{url}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def get_cached_url(original_url: str) -> str:
    """Return a previously resolved URL from the cache, or '' if not found."""
    try:
        con = get_db()
        cur = con.cursor()
        cur.execute("SELECT resolved_url FROM url_cache WHERE original_url = ?", (original_url,))
        row = cur.fetchone()
        con.close()
        return row[0] if row else ""
    except Exception:
        return ""


def set_cached_url(original_url: str, resolved_url: str):
    """Persist an original → resolved URL mapping so future runs skip the HTTP hop."""
    try:
        con = get_db()
        cur = con.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO url_cache (original_url, resolved_url, cached_at) VALUES (?, ?, ?)",
            (original_url, resolved_url, datetime.datetime.utcnow().isoformat()),
        )
        con.commit()
        con.close()
    except Exception:
        pass


def save_deal(deal):
    con = get_db()
    cur = con.cursor()
    now = datetime.datetime.utcnow().isoformat()

    cur.execute("""
        INSERT INTO deals (id, title, url, source, category, score,
                           price_now, price_was, discount, summary,
                           image_url, first_seen, last_seen)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            score=excluded.score,
            image_url=COALESCE(NULLIF(excluded.image_url,''), image_url),
            last_seen=excluded.last_seen
    """, (
        deal["id"], deal["title"], deal["url"], deal["source"],
        deal["category"], deal["score"], deal.get("price_now"),
        deal.get("price_was"), deal.get("discount"), deal.get("summary", ""),
        deal.get("image_url", ""),
        now, now,
    ))
    con.commit()
    con.close()

    # Record price point for history tracking
    if deal.get("price_now"):
        record_price_history(deal["id"], deal["price_now"])


# ─────────────────────────────────────────────────────────────────
#  DEAL EXPIRY CHECKER
# ─────────────────────────────────────────────────────────────────

# Text patterns that indicate a deal/item is no longer available
_SOLD_OUT_PATTERNS = [
    "currently unavailable",
    "this item is no longer available",
    "item not available",
    "sold out",
    "out of stock",
    "this deal has ended",
    "this deal has expired",
    "deal expired",
    "offer expired",
    "offer has ended",
    "no longer available",
    "page not found",
    "product not found",
    "item is unavailable",
    "temporarily out of stock",  # treat as expired for alerting purposes
    "we're sorry, this deal",
    "this offer is no longer valid",
    "listing has ended",          # eBay
    "this listing was ended",     # eBay
    "join to see price",          # Sam's Club login wall = effectively unavailable
]


def check_deal_alive(url: str) -> str:
    """
    Check whether a merchant deal URL is still live.

    Returns:
        "active"   — URL is reachable, no sold-out signals found
        "gone"     — HTTP 404/410/403: page simply doesn't exist anymore (hide entirely)
        "expired"  — sold-out / unavailable text detected on page (show in expired section)
        "unknown"  — Network error or non-merchant URL; keep as-is
    """
    if not url or not url.startswith("http"):
        return "unknown"

    headers = {"User-Agent": CONFIG["settings"]["user_agent"]}
    timeout = CONFIG["settings"].get("request_timeout", 10)

    try:
        # Step 1: HEAD request — fast, catches 404/410 without downloading the page
        resp = requests.head(url, headers=headers, timeout=timeout,
                             allow_redirects=True)
        if resp.status_code in (404, 410, 403):
            return "gone"   # Page is completely gone — hide from site entirely

        # Step 2: For 200 responses on merchant pages, fetch a snippet of HTML
        # and scan for sold-out / unavailable language.
        # We stream only the first 50 KB to keep it fast.
        resp = requests.get(url, headers=headers, timeout=timeout,
                            stream=True)
        chunk = b""
        for block in resp.iter_content(chunk_size=4096):
            chunk += block
            if len(chunk) >= 51200:   # 50 KB is enough to catch above-fold signals
                break

        page_text = chunk.decode("utf-8", errors="ignore").lower()

        for pattern in _SOLD_OUT_PATTERNS:
            if pattern in page_text:
                log.info(f"    ⚠️  Deal expired ('{pattern}' found): {url[:70]}")
                return "expired"

        return "active"

    except requests.RequestException:
        return "unknown"   # Network timeout / DNS fail — don't mark as expired


def expire_stale_deals():
    """
    Scan all 'active' deals in the database that haven't been checked today
    and mark expired ones. Called once per DealRadar scan.

    Deals are checked at most once per day to avoid hammering merchant sites.
    """
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    con = get_db()
    cur = con.cursor()

    rows = cur.execute("""
        SELECT id, url FROM deals
        WHERE status = 'active'
          AND (checked_at = '' OR checked_at < ?)
        ORDER BY last_seen DESC
    """, (today,)).fetchall()

    if not rows:
        con.close()
        return

    log.info(f"🔍 Checking expiry for {len(rows)} active deals…")
    expired_count = 0
    gone_count = 0

    for deal_id_str, url in rows:
        status = check_deal_alive(url)
        cur.execute(
            "UPDATE deals SET status=?, checked_at=? WHERE id=?",
            (status, today, deal_id_str)
        )
        if status == "expired":
            expired_count += 1
        elif status == "gone":
            gone_count += 1

    con.commit()
    con.close()

    if expired_count or gone_count:
        log.info(f"    ❌ Marked {expired_count} deal(s) as expired, {gone_count} as gone (error page).")
    else:
        log.info(f"    ✅ All checked deals still active.")


def record_price_history(deal_id_str: str, price: float):
    """Store one price data point per deal per day for the price history chart."""
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    con = get_db()
    cur = con.cursor()
    try:
        cur.execute(
            "INSERT OR IGNORE INTO price_history (deal_id, price, date) VALUES (?,?,?)",
            (deal_id_str, price, today)
        )
        con.commit()
    except Exception:
        pass
    finally:
        con.close()


def export_deals_json(deals: list, output_dir: str = "."):
    """
    Write deals.json and price_history/<deal_id>.json for the website.
    Call this at the end of each scan so the static site always has fresh data.

    Output layout (relative to output_dir):
        deals.json                     — all recent deals (for the homepage grid)
        price_history/<deal_id>.json   — [{date, price}, ...] sorted by date
    """
    import json, os

    # ── Pull live status from DB so expired deals are reflected ──────────────
    con_s = get_db()
    cur_s = con_s.cursor()
    status_map = {}
    for row in cur_s.execute("SELECT id, status FROM deals").fetchall():
        status_map[row[0]] = row[1]
    con_s.close()

    # ── deals.json ────────────────────────────────────────────────────────────
    deals_payload = {
        "version":   1,
        "generated": datetime.datetime.utcnow().strftime("%Y-%m-%d"),
        "deals": []
    }

    active_count = 0
    expired_count = 0

    # Hosts that are never useful as deal destinations (e.g. Slickdeals app redirects)
    _JUNK_EXPORT_HOSTS = {"play.google.com", "apps.apple.com"}

    for d in deals:
        did    = d.get("id", "")
        status = status_map.get(did, d.get("status", "active"))

        # "gone" = HTTP error page — exclude entirely, don't show on site at all
        if status == "gone":
            continue

        # Skip deals whose URL resolved to a junk/redirect destination
        deal_url = d.get("url", "") or ""
        if _host(deal_url) in _JUNK_EXPORT_HOSTS:
            continue

        is_exp = (status == "expired")

        if is_exp:
            expired_count += 1
        else:
            active_count += 1

        deals_payload["deals"].append({
            "id":               did,
            "title":            d.get("title", ""),
            "price":            d.get("price_now"),
            "original_price":   d.get("price_was"),
            "discount_percent": int(d.get("discount") or 0),
            "store":            d.get("source", ""),
            "badge":            "HOT" if (d.get("score") or 0) >= 75 and not is_exp else ("EXPIRED" if is_exp else ""),
            "category":         d.get("category", ""),
            "merchant_url":     d.get("url", ""),
            "image_url":        d.get("image_url", ""),
            "description":      d.get("summary", ""),
            "specs":            {},
            "upvotes":          d.get("score", 0),
            "downvotes":        0,
            "comment_count":    0,
            "posted_date":      (d.get("first_seen") or "")[:10],
            "expires":          None,
            "expired":          is_exp,
            "status":           status,
        })

    deals_path = os.path.join(output_dir, "deals.json")
    with open(deals_path, "w", encoding="utf-8") as f:
        json.dump(deals_payload, f, indent=2, ensure_ascii=False)
    log.info(f"Exported {len(deals)} deals → {deals_path}")

    # ── price_history/<id>.json ────────────────────────────────────────────────
    ph_dir = os.path.join(output_dir, "price_history")
    os.makedirs(ph_dir, exist_ok=True)

    con = get_db()
    cur = con.cursor()
    deal_ids = [d["id"] for d in deals if d.get("id")]
    for did in deal_ids:
        rows = cur.execute(
            "SELECT date, price FROM price_history WHERE deal_id=? ORDER BY date ASC",
            (did,)
        ).fetchall()
        if rows:
            ph_path = os.path.join(ph_dir, f"{did}.json")
            with open(ph_path, "w", encoding="utf-8") as f:
                json.dump([{"date": r[0], "price": r[1]} for r in rows], f)
    con.close()
    log.info(f"Exported price history for {len(deal_ids)} deals → {ph_dir}/")


def already_alerted(deal_id_str, hours=None):
    """Return True if this deal was already alerted within resend_hours."""
    hours = hours or CONFIG["settings"]["resend_hours"]
    con = get_db()
    cur = con.cursor()
    cutoff = (datetime.datetime.utcnow() -
              datetime.timedelta(hours=hours)).isoformat()
    cur.execute(
        "SELECT 1 FROM alerts_sent WHERE deal_id=? AND sent_at > ?",
        (deal_id_str, cutoff)
    )
    result = cur.fetchone()
    con.close()
    return result is not None


def mark_alerted(deal_id_str, alert_type="hot"):
    con = get_db()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO alerts_sent (deal_id, sent_at, alert_type) VALUES (?,?,?)",
        (deal_id_str, datetime.datetime.utcnow().isoformat(), alert_type)
    )
    con.commit()
    con.close()


# ─────────────────────────────────────────────────────────────────
#  RSS FETCHER
# ─────────────────────────────────────────────────────────────────
def fetch_feed(feed_cfg):
    """Fetch and parse a single RSS feed. Returns list of raw entries."""
    url  = feed_cfg["url"]
    name = feed_cfg["name"]

    headers = {"User-Agent": CONFIG["settings"]["user_agent"]}
    timeout = CONFIG["settings"]["request_timeout"]

    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        log.info(f"  {name}: {len(parsed.entries)} entries")
        return parsed.entries
    except Exception as e:
        log.warning(f"  {name}: FAILED — {e}")
        return []


# ─────────────────────────────────────────────────────────────────
#  PRICE EXTRACTOR
# ─────────────────────────────────────────────────────────────────
PRICE_RE = re.compile(r'\$\s?([\d,]+(?:\.\d{2})?)')

def extract_prices(text):
    """
    Try to pull current and original prices from deal text.
    Returns (price_now, price_was, discount_pct) or (None, None, None).
    """
    if not text:
        return None, None, None

    prices = []
    for m in PRICE_RE.finditer(text):
        try:
            val = float(m.group(1).replace(",", ""))
            if val > 0:
                prices.append(val)
        except ValueError:
            pass

    if len(prices) >= 2:
        prices.sort()
        price_now = prices[0]
        price_was = prices[-1]
        if price_was > price_now:
            discount = round((price_was - price_now) / price_was * 100, 1)
            return price_now, price_was, discount

    return None, None, None


# ─────────────────────────────────────────────────────────────────
#  DEAL SCORER
# ─────────────────────────────────────────────────────────────────
def score_deal(title, summary, category, discount_pct, price_now, price_was, priority):
    """
    Score a deal 0–100 based on:
      - Discount depth         (0–40 pts)
      - Keyword signals        (±25 pts)
      - Category weight        (multiplier)
      - Source priority        (0–10 pts)
      - Price drop absolute    (0–10 pts)
    """
    score = 0
    text  = f"{title} {summary}".lower()

    # ── Discount score (0–40 pts) ──────────────────────────────
    if discount_pct:
        if   discount_pct >= 70: score += 40
        elif discount_pct >= 50: score += 32
        elif discount_pct >= 40: score += 25
        elif discount_pct >= 30: score += 18
        elif discount_pct >= 20: score += 12
        elif discount_pct >= 15: score += 6

    # ── Absolute price drop (0–10 pts) ────────────────────────
    if price_now and price_was:
        drop = price_was - price_now
        if   drop >= 200: score += 10
        elif drop >= 100: score += 7
        elif drop >= 50:  score += 5
        elif drop >= 20:  score += 3
        elif drop >= 5:   score += 1

    # ── Keyword boosts / penalties ────────────────────────────
    for kw, boost in BOOST_KEYWORDS.items():
        if kw in text:
            score += boost

    for kw, penalty in PENALTY_KEYWORDS.items():
        if kw in text:
            score += penalty  # penalty values are negative

    # ── Category weight multiplier ────────────────────────────
    weight = CONFIG["category_weights"].get(category, 1.0)
    score  = int(score * weight)

    # ── Source priority bonus ─────────────────────────────────
    priority_bonus = {"high": 10, "medium": 5, "low": 0}
    score += priority_bonus.get(priority, 0)

    return max(0, min(100, score))


# ─────────────────────────────────────────────────────────────────
#  DEAL PARSER
# ─────────────────────────────────────────────────────────────────
def parse_entry(entry, feed_cfg):
    """Convert a raw feedparser entry into a normalised deal dict."""
    title   = getattr(entry, "title",   "") or ""
    url     = getattr(entry, "link",    "") or ""
    summary = getattr(entry, "summary", "") or ""

    # Strip HTML tags from summary
    summary = BeautifulSoup(summary, "html.parser").get_text(separator=" ")
    summary = re.sub(r'\s+', ' ', summary).strip()[:500]

    if not title or not url:
        return None

    # ── Resolve aggregator links → direct merchant URL ─────────────
    # e.g.  slickdeals.net/f/12345  →  amazon.com/dp/B0ABC123
    if _is_aggregator(url):
        if not CONFIG["settings"].get("resolve_merchant_urls", True):
            return None   # Resolution disabled — skip all aggregator-sourced deals
        resolved = resolve_merchant_url(url, entry=entry)
        if resolved != url:
            log.info(f"    🔗 {feed_cfg['name']}: {url[:60]}… → {resolved[:60]}")
        url = resolved
        # If still an aggregator after resolution — couldn't find the merchant link
        # Don't publish a broken or middleman link; skip this deal entirely
        if _is_aggregator(url):
            log.debug(f"    ⛔ Skipping: unresolved aggregator URL for '{title[:50]}'")
            return None

    # ── Reject deals that resolved to a junk/redirect destination ──
    # play.google.com = Slickdeals app redirect, not a real deal page
    _JUNK_HOSTS = {"play.google.com", "apps.apple.com"}
    if _host(url) in _JUNK_HOSTS:
        log.debug(f"    ⛔ Skipping: URL resolved to junk host '{_host(url)}' for '{title[:50]}'")
        return None

    # ── Freshness filter: drop entries older than max_age_days ────
    max_age = CONFIG["settings"].get("max_age_days", 3)
    pub_time = (
        getattr(entry, "published_parsed", None) or
        getattr(entry, "updated_parsed",   None)
    )
    if pub_time:
        try:
            pub_dt   = datetime.datetime(*pub_time[:6])
            age_days = (datetime.datetime.utcnow() - pub_dt).days
            if age_days > max_age:
                return None   # Too old — skip silently
        except Exception:
            pass  # Malformed date — let the entry through

    # Price extraction
    full_text   = f"{title} {summary}"
    price_now, price_was, discount = extract_prices(full_text)

    # Filter out non-deals with tiny discounts
    if discount and discount < CONFIG["thresholds"]["min_discount_pct"]:
        if not any(kw in full_text.lower() for kw in ["bank bonus", "checking", "savings bonus", "credit card bonus"]):
            return None

    if price_now and price_was:
        drop = price_was - price_now
        if drop < CONFIG["thresholds"]["min_price_drop"]:
            return None

    category = feed_cfg["category"]
    priority = feed_cfg.get("priority", "medium")

    score = score_deal(title, summary, category, discount, price_now, price_was, priority)

    # ── Fetch real product image from the merchant listing ─────────
    # Only attempt if we have a direct merchant URL (skip aggregators)
    image_url = ""
    if _is_merchant(url):
        image_url = fetch_product_image(url)
        if image_url:
            log.info(f"    🖼  Image found for: {title[:60]}")
        else:
            log.debug(f"    🖼  No image found for: {title[:60]}")

    return {
        "id":          deal_id(title, url),
        "title":       title[:200],
        "url":         url,          # always the direct merchant URL after resolution
        "source":      feed_cfg["name"],
        "category":    category,
        "score":       score,
        "price_now":   price_now,
        "price_was":   price_was,
        "discount":    discount,
        "summary":     summary,
        "image_url":   image_url,    # real listing photo, empty string if unavailable
        "is_merchant": _is_merchant(url),
    }


# ─────────────────────────────────────────────────────────────────
#  EMAIL NOTIFIER
# ─────────────────────────────────────────────────────────────────
def _build_deal_html(deal, badge=""):
    """Render a single deal as an HTML email block."""
    price_str = ""
    if deal.get("price_now") and deal.get("price_was"):
        price_str = (
            f"<span style='font-size:20px;font-weight:800;color:#1E1E2E;'>"
            f"${deal['price_now']:.2f}</span>"
            f" <span style='font-size:13px;color:#AAA;text-decoration:line-through;'>"
            f"${deal['price_was']:.2f}</span>"
        )
        if deal.get("discount"):
            price_str += (
                f" <span style='background:#FFF0E6;color:#FF6B00;font-size:12px;"
                f"font-weight:700;padding:2px 7px;border-radius:4px;'>"
                f"-{deal['discount']}%</span>"
            )

    score_color = "#00A86B" if deal["score"] >= 75 else ("#FF6B00" if deal["score"] >= 50 else "#1A73E8")

    return f"""
    <div style="border:1px solid #E2E8F0;border-radius:10px;overflow:hidden;
                margin-bottom:14px;background:#fff;">
      {f'''<a href="{deal['url']}" style="display:block;">
        <img src="{deal['image_url']}" alt="{deal['title'][:60]}"
             style="width:100%;height:180px;object-fit:contain;
                    background:#F7F9FC;padding:8px;box-sizing:border-box;">
      </a>''' if deal.get('image_url') else ''}
      <div style="padding:16px;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;">
          <div style="flex:1;">
            <div style="font-size:11px;color:#1A73E8;font-weight:700;
                        text-transform:uppercase;letter-spacing:.6px;margin-bottom:4px;">
              {deal['source']} &nbsp;·&nbsp; {deal['category'].upper()}
              {f'&nbsp;·&nbsp; <span style="color:#FF6B00;">{badge}</span>' if badge else ''}
            </div>
            <a href="{deal['url']}" style="color:#1E1E2E;font-size:14px;
               font-weight:600;text-decoration:none;line-height:1.4;">
              {deal['title']}
            </a>
            <div style="margin-top:8px;">{price_str}</div>
            {f'<div style="font-size:12px;color:#666;margin-top:6px;">{deal["summary"][:180]}…</div>' if deal.get("summary") else ""}
          </div>
          <div style="text-align:center;margin-left:16px;flex-shrink:0;">
            <div style="font-size:22px;font-weight:800;color:{score_color};">{deal['score']}</div>
            <div style="font-size:10px;color:#AAA;text-transform:uppercase;">Score</div>
          </div>
        </div>
        <div style="margin-top:10px;">
          <a href="{deal['url']}" style="background:#1A73E8;color:#fff;padding:8px 18px;
             border-radius:6px;font-size:13px;font-weight:600;text-decoration:none;">
            View Deal →
          </a>
        </div>
      </div>
    </div>
    """


def _email_wrapper(body_html, subject):
    """Wrap deal HTML in a polished email shell."""
    now = datetime.datetime.now().strftime("%B %d, %Y at %I:%M %p")
    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#F7F9FC;font-family:Arial,sans-serif;">
  <div style="max-width:600px;margin:0 auto;padding:20px;">

    <!-- Header -->
    <div style="background:linear-gradient(135deg,#1A73E8,#0D47A1);border-radius:12px;
                padding:24px;text-align:center;margin-bottom:20px;">
      <div style="font-size:28px;font-weight:800;color:#fff;letter-spacing:-0.5px;">
        Deal<span style="color:#FF6B00;">Pulses</span>
      </div>
      <div style="font-size:13px;color:rgba(255,255,255,.8);margin-top:4px;">
        DealRadar™ Alert &nbsp;·&nbsp; {now}
      </div>
    </div>

    <!-- Body -->
    {body_html}

    <!-- Footer -->
    <div style="text-align:center;font-size:11px;color:#AAA;margin-top:24px;
                padding-top:16px;border-top:1px solid #E2E8F0;">
      You're receiving this because you set up DealRadar™ alerts.<br/>
      <a href="https://dealpulses.com" style="color:#1A73E8;">dealpulses.com</a>
      &nbsp;·&nbsp; Unsubscribe
    </div>

  </div>
</body>
</html>
"""


def send_email(to_list, subject, body_html):
    """Send an HTML email via SMTP."""
    cfg = CONFIG["email"]
    if not cfg["enabled"]:
        log.info("Email disabled in config — skipping send.")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"DealPulses DealRadar <{cfg['sender_email']}>"
        msg["To"]      = ", ".join(to_list)
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
            server.starttls()
            server.login(cfg["sender_email"], cfg["sender_pass"])
            server.sendmail(cfg["sender_email"], to_list, msg.as_string())

        log.info(f"Email sent → {to_list}  |  {subject}")
        return True

    except Exception as e:
        log.error(f"Email FAILED: {e}")
        return False


def send_hot_alert(deal):
    """Instant email for a single HOT deal."""
    badge   = "🔥 HOT DEAL"
    body    = f"""
    <div style="background:#FFF3F0;border:2px solid #FF6B00;border-radius:10px;
                padding:14px;margin-bottom:16px;text-align:center;">
      <div style="font-size:16px;font-weight:800;color:#FF6B00;">
        🔥 HOT DEAL ALERT — Score {deal['score']}/100
      </div>
      <div style="font-size:12px;color:#666;margin-top:4px;">
        DealRadar™ flagged this as a top deal right now
      </div>
    </div>
    {_build_deal_html(deal, badge)}
    """
    subject = f"🔥 Hot Deal: {deal['title'][:60]}{'…' if len(deal['title'])>60 else ''}"
    return send_email(CONFIG["email"]["alert_to"], subject, _email_wrapper(body, subject))


def send_digest(deals):
    """Send a daily digest email with the top deals."""
    if not deals:
        log.info("No deals to include in digest.")
        return

    max_deals = CONFIG["settings"]["max_deals_digest"]
    top_deals = sorted(deals, key=lambda d: d["score"], reverse=True)[:max_deals]

    deals_html = "".join(_build_deal_html(d) for d in top_deals)
    count      = len(top_deals)
    avg_score  = int(sum(d["score"] for d in top_deals) / count) if count else 0

    body = f"""
    <div style="background:#E8F0FE;border-radius:10px;padding:14px;
                margin-bottom:16px;text-align:center;">
      <div style="font-size:16px;font-weight:800;color:#1A73E8;">
        📋 Today's Deal Digest — {count} Top Deals Found
      </div>
      <div style="font-size:12px;color:#666;margin-top:4px;">
        Average deal score: {avg_score}/100 &nbsp;·&nbsp;
        {datetime.date.today().strftime("%A, %B %d, %Y")}
      </div>
    </div>
    {deals_html}
    """
    subject = f"📋 DealPulses Daily Digest — {count} deals, best score {top_deals[0]['score']}/100"
    return send_email(CONFIG["email"]["digest_to"], subject, _email_wrapper(body, subject))


# ─────────────────────────────────────────────────────────────────
#  MAIN RADAR LOOP
# ─────────────────────────────────────────────────────────────────
def run_radar(force_digest=False, top_n=None):
    """
    Main entry point:
    1. Fetch all RSS feeds
    2. Parse and score every entry
    3. Save new deals to DB
    4. Send instant alerts for HOT deals
    5. (Optionally) send digest
    """
    log.info("=" * 60)
    log.info("DealPulses DealRadar™ — starting scan")
    log.info(f"Monitoring {len([f for f in FEEDS if f.get('rss', True)])} RSS feeds")
    log.info("=" * 60)

    init_db()

    all_deals   = []
    hot_deals   = []
    hot_thresh  = CONFIG["thresholds"]["hot_alert_score"]
    min_thresh  = CONFIG["thresholds"]["digest_min_score"]

    # ── Phase 1: Collect all feed entries ─────────────────────────
    all_entries = []
    for feed_cfg in FEEDS:
        if not feed_cfg.get("rss", True):
            continue  # Skip non-RSS sources
        entries = fetch_feed(feed_cfg)
        for entry in entries:
            all_entries.append((entry, feed_cfg))

    # ── Phase 2: Parse entries in parallel (I/O-bound) ────────────
    # URL resolution + image fetching are network calls — running 20
    # at once collapses ~1000s of sequential wait into ~50s.
    max_workers = CONFIG["settings"].get("parse_workers", 20)
    log.info(f"Parsing {len(all_entries)} entries with {max_workers} parallel workers…")
    parsed_deals = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(parse_entry, e, fc): (e, fc)
            for e, fc in all_entries
        }
        for future in as_completed(futures):
            try:
                deal = future.result()
            except Exception as exc:
                log.debug(f"parse_entry raised: {exc}")
                continue
            if deal:
                parsed_deals.append(deal)

    # ── Phase 3: Save to DB and classify (sequential — SQLite writes)
    for deal in parsed_deals:
        save_deal(deal)

        if deal["score"] >= min_thresh:
            all_deals.append(deal)

        # Fire instant alert for HOT deals not yet alerted
        if deal["score"] >= hot_thresh and not already_alerted(deal["id"]):
            hot_deals.append(deal)

    log.info("-" * 60)
    log.info(f"Scan complete. Total qualifying deals: {len(all_deals)}")
    log.info(f"HOT deals to alert: {len(hot_deals)}")

    # ── Print to terminal if --top flag ───────────────────────
    if top_n:
        print(f"\n{'─'*60}")
        print(f"  TOP {top_n} DEALS  (sorted by score)")
        print(f"{'─'*60}")
        top = sorted(all_deals, key=lambda d: d["score"], reverse=True)[:top_n]
        for i, d in enumerate(top, 1):
            price_str = ""
            if d.get("price_now") and d.get("price_was"):
                price_str = f"  ${d['price_now']:.0f} (was ${d['price_was']:.0f}, -{d['discount']:.0f}%)"
            print(f"\n  #{i}  [{d['score']}/100] {d['source']} — {d['category'].upper()}")
            print(f"      {d['title'][:80]}")
            print(f"     {price_str}  |  {d['url'][:60]}")
        print(f"\n{'─'*60}\n")
        return

    # ── Send HOT deal instant alerts ──────────────────────────
    for deal in hot_deals:
        log.info(f"HOT ALERT: [{deal['score']}/100] {deal['title'][:60]}")
        if send_hot_alert(deal):
            mark_alerted(deal["id"], "hot")
        time.sleep(1)  # Be gentle on SMTP

    # ── Send digest if forced ─────────────────────────────────
    if force_digest:
        log.info("Sending digest email...")
        send_digest(all_deals)

    # Check existing deals for expiry (once per day per deal)
    try:
        expire_stale_deals()
    except Exception as exc:
        log.warning(f"Expiry check failed: {exc}")

    # Export deals.json + price_history/*.json for the website.
    # Query the FULL DB (not just this scan's batch) so the site shows
    # all recent deals accumulated over many runs, not only 3 new ones.
    if CONFIG["settings"].get("export_json", True):
        export_dir = CONFIG["settings"].get("export_json_dir", ".")
        try:
            max_age = CONFIG["settings"].get("max_age_days", 3)
            cutoff  = (datetime.datetime.utcnow() -
                       datetime.timedelta(days=max_age)).strftime("%Y-%m-%dT%H:%M:%S")
            con_e = get_db()
            cur_e = con_e.cursor()
            rows  = cur_e.execute("""
                SELECT id, title, url, source, category, score,
                       price_now, price_was, discount, summary,
                       first_seen, last_seen, image_url, status
                FROM   deals
                WHERE  last_seen >= ? AND status != 'gone'
                ORDER  BY score DESC, last_seen DESC
            """, (cutoff,)).fetchall()
            con_e.close()
            db_deals = [
                {
                    "id":         r[0],  "title":     r[1],  "url":      r[2],
                    "source":     r[3],  "category":  r[4],  "score":    r[5],
                    "price_now":  r[6],  "price_was": r[7],  "discount": r[8],
                    "summary":    r[9],  "first_seen":r[10], "last_seen":r[11],
                    "image_url":  r[12], "status":    r[13],
                }
                for r in rows
            ]
            log.info(f"Exporting {len(db_deals)} deals from DB (last {max_age} days)…")
            export_deals_json(db_deals, output_dir=export_dir)
        except Exception as exc:
            log.warning(f"JSON export failed: {exc}")

    log.info("DealRadar™ scan finished.")
    return all_deals


# ─────────────────────────────────────────────────────────────────
#  TEST MODE
# ─────────────────────────────────────────────────────────────────
def run_test():
    """Send a test email to verify SMTP config is working."""
    log.info("Running email config test...")
    body = """
    <div style="background:#E8F8F1;border-radius:10px;padding:20px;text-align:center;">
      <div style="font-size:24px;margin-bottom:8px;">✅</div>
      <div style="font-size:16px;font-weight:700;color:#1E1E2E;">
        DealRadar™ Email Config Working!
      </div>
      <div style="font-size:13px;color:#666;margin-top:8px;">
        Your DealPulses deal alerts are set up and ready to fire.<br/>
        Hot deals scoring above {thresh}/100 will trigger instant emails.
      </div>
    </div>
    """.format(thresh=CONFIG["thresholds"]["hot_alert_score"])
    subject = "✅ DealPulses DealRadar™ — Email Config Test Passed"
    html = _email_wrapper(body, subject)

    all_recipients = list(set(CONFIG["email"]["alert_to"] + CONFIG["email"]["digest_to"]))
    if send_email(all_recipients, subject, html):
        log.info("Test email sent successfully! Check your inbox.")
    else:
        log.error("Test email FAILED. Check SMTP settings in CONFIG.")


# ─────────────────────────────────────────────────────────────────
#  CLI ENTRY POINT
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="DealPulses DealRadar™ — Real-time deal monitoring engine"
    )
    parser.add_argument(
        "--digest", action="store_true",
        help="Force-send daily digest email after scanning"
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Send a test email to verify SMTP config"
    )
    parser.add_argument(
        "--top", type=int, metavar="N",
        help="Print top N deals to terminal (no email sent)"
    )
    args = parser.parse_args()

    if args.test:
        run_test()
    elif args.top:
        run_radar(top_n=args.top)
    else:
        run_radar(force_digest=args.digest)
