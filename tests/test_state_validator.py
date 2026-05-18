import unittest
from unittest.mock import MagicMock, patch
import os
import json
import tempfile
import shutil
from datetime import datetime, timezone, timedelta

from src.state_validator import validate_transition, StateValidationError
from src.analysis_agent import AnalysisAgent

class TestStateValidator(unittest.TestCase):
    def test_valid_transition(self):
        # Remaining in the same regime, count must increment
        prev = {"previous_market_regime": "Bear", "bars_in_trend_count": 5, "last_unique_id": "ID_1"}
        new = {"previous_market_regime": "Bear", "bars_in_trend_count": 6, "last_unique_id": "ID_2"}
        self.assertTrue(validate_transition(prev, new))

    def test_invalid_linear_progression(self):
        # Count jumps by more than 1
        prev = {"previous_market_regime": "Bear", "bars_in_trend_count": 5, "last_unique_id": "ID_1"}
        new = {"previous_market_regime": "Bear", "bars_in_trend_count": 7, "last_unique_id": "ID_2"}
        with self.assertRaises(StateValidationError):
            validate_transition(prev, new)

    def test_valid_regime_reset(self):
        # Regime changes, count resets to 1
        prev = {"previous_market_regime": "Bear", "bars_in_trend_count": 5, "last_unique_id": "ID_1"}
        new = {"previous_market_regime": "Bull", "bars_in_trend_count": 1, "last_unique_id": "ID_2"}
        self.assertTrue(validate_transition(prev, new))

    def test_invalid_regime_reset(self):
        # Regime changes, but count does not reset to 1
        prev = {"previous_market_regime": "Bear", "bars_in_trend_count": 5, "last_unique_id": "ID_1"}
        new = {"previous_market_regime": "Bull", "bars_in_trend_count": 2, "last_unique_id": "ID_2"}
        with self.assertRaises(StateValidationError):
            validate_transition(prev, new)

    def test_duplicate_bar(self):
        # Same unique ID processed twice
        prev = {"previous_market_regime": "Bear", "bars_in_trend_count": 5, "last_unique_id": "ID_1"}
        new = {"previous_market_regime": "Bear", "bars_in_trend_count": 6, "last_unique_id": "ID_1"}
        with self.assertRaises(StateValidationError):
            validate_transition(prev, new)

    def test_uninitialized_corruption(self):
        # Offline corruption: count loaded but unique ID is None
        prev = {"previous_market_regime": "Bear", "bars_in_trend_count": 99, "last_unique_id": None}
        new = {"previous_market_regime": "Bear", "bars_in_trend_count": 100, "last_unique_id": "ID_1"}
        with self.assertRaises(StateValidationError):
            validate_transition(prev, new)


class TestAnalysisAgentIntegration(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for out files to run offline
        self.test_dir = tempfile.mkdtemp()
        
    def tearDown(self):
        # Remove temporary directory
        shutil.rmtree(self.test_dir)

    @patch("src.analysis_agent.StockHistoricalDataClient")
    def test_self_healing_rebuilds_ledger(self, mock_client_cls):
        # Setup mock historical data response from Alpaca
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        
        # Construct 140 dummy bars
        dummy_bars = []
        base_time = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
        for i in range(140):
            bar = MagicMock()
            # Let's make timestamps distinct using timedelta
            bar.timestamp = base_time + timedelta(minutes=i)
            # Create price movement (sma_5 > sma_20 -> Bull)
            # Make price climb so closes[-5:] > closes[-20:]
            bar.close = 100.0 + i * 1.0
            bar.high = bar.close + 1.0
            bar.low = bar.close - 1.0
            bar.volume = 1000.0
            bar.trade_count = 100.0 + i
            bar.sma_macro = 100.0 + i * 1.0
            bar.symbol = "SPY"
            dummy_bars.append(bar)
            
        mock_response = MagicMock()
        mock_response.data = {"SPY": dummy_bars}
        mock_client.get_stock_bars.return_value = mock_response
        
        # Instantiate agent
        agent = AnalysisAgent(api_key="mock", secret_key="mock")
        # Override paths to use the temp directory
        agent.out_dir = self.test_dir
        agent.state_file = os.path.join(self.test_dir, "state.json")
        
        # Seed corrupted state.json (similar to fault injection harness)
        # SPY has Bear regime and trend count 99, but uninitialized unique ID
        corrupt_state = {
            "SPY": {
                "previous_market_regime": "Bear",
                "bars_in_trend_count": 99,
                "last_unique_id": None
            }
        }
        with open(agent.state_file, "w") as f:
            json.dump(corrupt_state, f)
            
        # Run generate_proposals - this should trigger the validation error
        # because the loaded state is inconsistent, and then self-heal
        proposals = agent.generate_proposals(["SPY"])
        
        # Assert proposals compiled and contains SPY
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["symbol"], "SPY")
        
        # The validator should have healed the state
        # The expected regime is Bull (since price is climbing: closes are climbing 100 to 150)
        self.assertEqual(proposals[0]["validator_status"], "REBUILT")
        self.assertEqual(proposals[0]["market_regime"], "Bull")
        
        # Check that the healed state is persisted in state.json
        with open(agent.state_file, "r") as f:
            saved_state = json.load(f)
        self.assertEqual(saved_state["SPY"]["previous_market_regime"], "Bull")
        self.assertGreater(saved_state["SPY"]["bars_in_trend_count"], 0)
        
        # Verify STATE_RECONSTRUCTION log is written to trading_journal.jsonl in the temp dir
        journal_file = os.path.join(self.test_dir, "trading_journal.jsonl")
        self.assertTrue(os.path.exists(journal_file))
        
        with open(journal_file, "r") as f:
            lines = f.readlines()
            
        self.assertGreaterEqual(len(lines), 1)
        log_entry = json.loads(lines[0])
        self.assertEqual(log_entry["event_type"], "STATE_RECONSTRUCTION")
        self.assertEqual(log_entry["symbol"], "SPY")
        self.assertIn("Inconsistent uninitialized state", log_entry["failed_assertion"])
        self.assertEqual(log_entry["corrupted_state_snapshot"]["REGIME"], "Bear")
        self.assertEqual(log_entry["corrupted_state_snapshot"]["COUNT"], 99)

if __name__ == "__main__":
    unittest.main()
