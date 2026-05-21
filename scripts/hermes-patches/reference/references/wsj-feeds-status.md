# WSJ RSS Feeds — Known Status (May 2026)

## feeds.a.dj.com RSS feeds are FROZEN

All WSJ RSS feeds at `feeds.a.dj.com` stopped updating in January 2025:
- `https://feeds.a.dj.com/rss/RSSWSJD.xml` — last article Jan 27, 2025, all marked "PAID"
- `https://feeds.a.dj.com/rss/RSSWorldNews.xml` — last article Jan 27, 2025
- `https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml` — last article Jan 24, 2025
- `https://feeds.a.dj.com/rss/RSSMarketsMain.xml` — last article Jan 27, 2025

These feeds are NOT maintained by WSJ. Do not add them to blogwatcher-cli — they will produce stale, months-old headlines.

## Subscriber Workaround

If Chief is a WSJ subscriber, use web search instead:
```
web_search "site:wsj.com top headlines today"
web_search "site:wsj.com business markets today"
```

Subscriber cookies handle authentication when clicking through. Headlines + URLs only — don't try to extract paywalled content.

## NYT RSS is ACTIVE

`https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml` — current, updated daily, rich summaries. The category tags in blogwatcher-cli (e.g., "International Relations, Politics") are topic tags, NOT paywall indicators. Include normally.
