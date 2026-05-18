# Alpaca Autonomous Agent Framework

A mathematically deterministic, agentic data collection and simulated trading framework built on top of the Alpaca API. 

This project is designed with strict modularity, separating pure-mathematical market analysis from risk consensus, state validation, and empirical post-replay backtesting. It prevents "narrative drift" through rigid mathematical heuristics and an immutable JSONL audit trail.

---

## 🏗️ Architecture & Core Components

The framework is broken down into highly specialized, decoupled modules:

### 1. The Autonomous Engine (`src/`)
* **`main.py`**: The orchestration loop that continuously runs the analysis and risk reviewer engines.
* **`analysis_agent.py`**: The core data engine. Computes 20-bar Simple Moving Averages (SMA), Volume SMAs, and 14-bar Average True Range (ATR). It establishes exact market regimes (Bull, Bear, Congestion) and assesses trend fatigue.
* **`risk_reviewer.py`**: Enforces strict risk constraints. Vetos trades if they violate dynamic position sizing rules (e.g., stopping trades if price is overextended beyond 2 ATRs from the mean).
* **`state_validator.py`**: A pure mathematical state transition gate. It protects against offline tampering, duplication (via unique Alpaca IDs), and ensures that trend counts increment or reset precisely according to the regime transition matrix.

### 2. Deterministic Validation & Backtesting (`src/validation/`)
A 100% offline, deterministic simulation framework designed to mathematically prove the predictive validity of the engine's heuristics.
* **`data_loader.py`**: Supports Mode A (mock deterministic bars with configurable anomalies like `flat_volume` or `price_spike`) and Mode B (live Alpaca historical data). Both output to a perfectly unified dictionary schema.
* **`replay_engine.py`**: Sequentially replays the data array, overriding the live agent to compute `+1`, `+3`, and `+10` empirical forward returns for every single proposal.
* **`outcome_tracker.py`**: Calculates the precise empirical future outcomes.
* **`statistical_analyzer.py`**: Parses the replay log, filters incomplete horizons, and computes:
  * **Confidence Stratification**: Mean and standard deviation of forward returns grouped by base confidence.
  * **Fatigue Validity Check**: The Pearson correlation coefficient ($r$) between the fatigue ratio and forward returns.
  * **Regime Split**: Distinct performance profiles across Bull, Bear, and Congestion regimes.

---

## 🚀 Getting Started

This project uses [Pixi](https://pixi.sh/) for deterministic dependency management. 

### Prerequisites
1. Install [Pixi](https://pixi.sh/).
2. Create an `.env` file in the root directory with your Alpaca API credentials:
   ```env
   ALPACA_API_KEY=your_api_key_here
   ALPACA_SECRET_KEY=your_secret_key_here
   ```

### Command Reference

The `pixi.toml` configuration exposes three simple task commands:

#### 1. Run the Live Production Loop
Continuously fetches the latest 15-minute bars and appends validated, risk-reviewed trade proposals to `out/trading_journal.jsonl`.
```bash
pixi run start
```

#### 2. Run the Offline Validation Replay
Generates a 100-bar deterministic mock simulation, replays the market tick-by-tick, tracks empirical forward returns, and runs the Statistical Analyzer script to output the *System Behavioral Calibration Report*.
```bash
pixi run analyze
```

#### 3. Run the Automated Unit Test Suite
Executes the extensive standard Python `unittest` suite (testing state transitions, self-healing corruption recovery, and Alpaca data loader schema swappability) entirely offline.
```bash
pixi run test
```

---

## 📊 Observability & Audit Trail

Every evaluation cycle outputs a completely standalone, parseable JSON object to `out/trading_journal.jsonl`. 
* **State Telemetry**: Standard entries include explicit `fatigue_ratio` decay limits and `transition_trajectory` paths.
* **Self-Healing Audit Logs**: If the `state_validator` detects corrupted local memory (e.g., offline file tampering), it instantly logs a `STATE_RECONSTRUCTION` JSON block capturing the exact state before mathematically rebuilding the ledger from historical data.