"""
Market-data recorder for Binance USD-M Futures perpetuals (default BTCUSDT, TradingView BTCUSDT.P).

Records closed candles for 5m, 15m, 1h, 4h, 1d and 1w, computes EMA50, EMA200,
RSI 14 and volume SMA 20 the way TradingView does, marks the owner's Strategy-1
"EMA Retest Zones" signals, and measures what price did afterwards.

Separate from the trading bot: own database (data/market_data.db), public
market data only, no API keys, no orders.
"""
