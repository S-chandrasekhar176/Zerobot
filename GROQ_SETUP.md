# ZeroBot — Groq AI Integration Guide

## What Groq Does for ZeroBot

Groq replaces two local rule-based gates with **LLaMA 3.3 70B** running at 500 tokens/sec:

| Gate | Without Groq | With Groq |
|------|-------------|-----------|
| **Gate 6 — ML Confidence** | `signal.confidence >= 62%` (blended XGBoost score) | LLM calculates confluence of strategy conf + sentiment + VIX penalty |
| **Gate 11 — News Sentiment** | Keyword matching ("fraud"→bearish, "dividend"→bullish) | LLM reads actual NSE headlines and understands **sector-specific** impact |

### Why this meaningfully improves P&L:
- **Keyword scoring misses context**: "rate cut" is bullish for HDFCBANK but bearish for ONGC's bond yields. LLM knows the difference.
- **Confluence boost**: When news strongly agrees with your signal, Groq raises `ml_conf` → Kelly gives you more size on high-conviction trades.
- **VIX-aware rejection**: At VIX > 20, Groq subtracts 0.15 from conf and may block marginal trades automatically.
- **Speed**: Groq's LPU delivers < 200ms per gate call — faster than your 60-second strategy cycle.

## Setup (Free — 14,400 calls/day)

1. Go to https://console.groq.com → create account → API Keys → Create Key
2. Copy the key (starts with `gsk_...`)
3. Open `config/.env` and add:
   ```
   GROQ_API_KEY=gsk_your_key_here
   ```
4. Restart ZeroBot — you'll see in startup logs:
   ```
   ✅  Groq AI (Gates 6+11)   LLaMA 3.3-70B active — llama-3.3-70b-versatile
   ```

## Fallback Behaviour
If Groq is not configured or the API call fails (timeout, rate limit), ZeroBot automatically falls back to local keyword scoring and XGBoost confidence. **Zero disruption.**

## Cost
- Free tier: **14,400 requests/day** 
- ZeroBot at maximum throughput (1 signal/min × 12 symbols) = ~720 Groq calls/6hr session
- You have **20× headroom** on the free tier. No card needed.
