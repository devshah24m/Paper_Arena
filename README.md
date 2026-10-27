# Paper Arena

Competitive paper-trading simulator on Angel One market depth.

## Run
    pip install -r requirements.txt
    cp .env.example .env      # fill Angel One credentials (or set MOCK_FEED=1)
    streamlit run app.py --server.address 0.0.0.0   # others on your network open http://<your-ip>:8501

## How fills work
- Market orders walk the live 5-level depth. Beyond it, depth is extrapolated (0.05% per level) up to the circuit limit.
- Every fill eats liquidity shared by ALL traders; it heals with a 90s half-life (IMPACT_HALF_LIFE_SEC).
- Limit orders rest and fill via live quotes; MIS auto square-off at 15:20 IST; day orders expire.
- No capital limit. Return % = net P&L / peak capital deployed.
- Charges: charges.py (brokerage, STT, exchange txn, SEBI, IPFT, stamp, GST, DP). Verify RATES at angelone.in/list-of-charges.

## Test
    python test_engine.py
