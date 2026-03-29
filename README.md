# 📡 MEXC Vision Signal Bot

An asynchronous Telegram bot for monitoring USDT futures pairs on the MEXC exchange.  
The bot tracks sharp price movements and overbought/oversold conditions using RSI — and sends signals with charts to Telegram.

---

## 🧠 How It Works

### Architecture: WebSocket + REST

```
WebSocket (all pairs) → RAM (tick deque)
         ↓ every 60 sec
    Filter 1: price movement over 15 min  ← from RAM, no REST
         ↓ passed only
    Filter 2: RSI 1h                     ← 1 REST call
         ↓ passed only
    Filter 3: RSI 15m                    ← 1 REST call
         ↓
   Signal → SQLite → Telegram (text + chart)
```

### Three Signal Filters

| # | Filter | Condition |
|---|--------|----------|
| 1 | Price movement | `abs(change_15m) >= 8%` |
| 2 | RSI 1h | `> 70` (overbought) or `< 30` (oversold) |
| 3 | RSI 15m | `> 70` or `< 30` |

All three filters must trigger simultaneously.

### Signal Direction

| Condition | Signal |
|----------|--------|
| Price up + RSI oversold | 🟢 LONG |
| Price down + RSI overbought | 🔴 SHORT |
| Ambiguous | Determined by price direction |

### Additional Analysis

- RSI divergence 5m (auto-detected)
- Chart: 5m candles (12h) + RSI + Volume

---

## ⚙️ Installation

```bash
git clone <repo>
cd mexc_vision
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## ▶️ Run

```bash
python main.py
```

---

## ⚠️ Notes

- Futures only
- 15 min warm-up
- Not a trading bot