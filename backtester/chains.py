"""Die drei auswertbaren Chains (Reihenfolge = Anzeige-Reihenfolge)."""
CHAINS = {
    "btc": {"name": "Bitcoin", "symbol": "BTC/USDT"},
    "sol": {"name": "Solana", "symbol": "SOL/USDT"},
    "eth": {"name": "Ethereum", "symbol": "ETH/USDT"},
}
CHAIN_CHOICES = [(k, v["name"]) for k, v in CHAINS.items()]

# Boersen (ccxt-IDs). Reihenfolge = Reihenfolge im Dropdown.
EXCHANGES = {
    "binance": "Binance",
    "kraken": "Kraken",
    "coinbaseexchange": "Coinbase",
    "bybit": "Bybit",
    "okx": "OKX",
    "bitstamp": "Bitstamp",
}
EXCHANGE_CHOICES = list(EXCHANGES.items())
# max. Kerzen je Abruf (Boersen begrenzen unterschiedlich)
EXCHANGE_LIMITS = {"binance": 1000, "kraken": 720, "coinbaseexchange": 300,
                   "bybit": 1000, "okx": 300, "bitstamp": 1000}
