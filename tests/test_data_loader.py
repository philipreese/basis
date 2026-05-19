import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

from src.validation.data_loader import generate_mock_bars, fetch_alpaca_bars

class TestDataLoaderSchema(unittest.TestCase):
    def setUp(self):
        self.expected_keys = {
            "timestamp", "open", "high", "low", "close", "volume", "trade_count", "data_provenance", "sma_macro",
            "vwap", "obv", "obv_sma20", "z_velocity", "atr_14", "rsc", "rsc_std_100", "z_rsc", "max_dd_rsc",
            "atr_paired", "close_paired"
        }
        
    def test_mode_a_mock_generator_schema(self):
        """Test Case 1: Execute Mode A and assert keys, shapes, and types."""
        bars = generate_mock_bars("SPY", length=100)
        self.assertEqual(len(bars), 100)
        
        for bar in bars:
            self.assertEqual(set(bar.keys()), self.expected_keys)
            self.assertIsInstance(bar["timestamp"], datetime)
            self.assertIsInstance(bar["open"], float)
            self.assertIsInstance(bar["high"], float)
            self.assertIsInstance(bar["low"], float)
            self.assertIsInstance(bar["close"], float)
            self.assertIsInstance(bar["volume"], float)
            self.assertIsInstance(bar["trade_count"], int)
            self.assertIsInstance(bar["sma_macro"], float)
            
    @patch("src.validation.data_loader.StockHistoricalDataClient")
    def test_mode_b_alpaca_normalization(self, mock_client_cls):
        """Test Case 2: Mock Alpaca client and verify normalization output."""
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        
        # Setup mock Alpaca bar object
        mock_bar = MagicMock()
        mock_bar.timestamp = datetime(2026, 5, 18, 14, 0, tzinfo=timezone.utc)
        mock_bar.open = 105.5
        mock_bar.high = 106.0
        mock_bar.low = 105.0
        mock_bar.close = 105.8
        mock_bar.volume = 50000.0
        mock_bar.trade_count = 120
        mock_bar.symbol = "SPY"
        
        mock_bar_qqq = MagicMock()
        mock_bar_qqq.timestamp = datetime(2026, 5, 18, 14, 0, tzinfo=timezone.utc)
        mock_bar_qqq.open = 105.5
        mock_bar_qqq.high = 106.0
        mock_bar_qqq.low = 105.0
        mock_bar_qqq.close = 105.8
        mock_bar_qqq.volume = 50000.0
        mock_bar_qqq.trade_count = 120
        mock_bar_qqq.symbol = "QQQ"
        
        mock_response = MagicMock()
        mock_response.data = {"SPY": [mock_bar], "QQQ": [mock_bar_qqq]}
        mock_client.get_stock_bars.return_value = mock_response
        
        # Execute loader Mode B
        bars = fetch_alpaca_bars(
            symbol="SPY",
            start_time=datetime.now() - timedelta(days=1),
            end_time=datetime.now(),
            client=mock_client
        )
        
        self.assertEqual(len(bars), 1)
        bar = bars[0]
        
        # Assert normalization schema and types match perfectly
        self.assertEqual(set(bar.keys()), self.expected_keys)
        self.assertEqual(bar["timestamp"], mock_bar.timestamp)
        self.assertEqual(bar["open"], 105.5)
        self.assertEqual(bar["high"], 106.0)
        self.assertEqual(bar["low"], 105.0)
        self.assertEqual(bar["close"], 105.8)
        self.assertEqual(bar["volume"], 50000.0)
        self.assertEqual(bar["trade_count"], 120)
        
    @patch("src.validation.data_loader.StockHistoricalDataClient")
    def test_mode_a_and_mode_b_swappability(self, mock_client_cls):
        """Test Case 3: Compare a sample bar from Mode A and Mode B side-by-side."""
        # 1. Get Mode A bar
        mode_a_bars = generate_mock_bars("SPY", length=1)
        self.assertEqual(len(mode_a_bars), 1)
        mode_a_bar = mode_a_bars[0]
        
        # 2. Get Mode B bar (mocked client)
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        
        mock_bar = MagicMock()
        mock_bar.timestamp = datetime(2026, 5, 18, 9, 30, tzinfo=timezone.utc)
        mock_bar.open = 99.95
        mock_bar.high = 100.15
        mock_bar.low = 99.75
        mock_bar.close = 100.0
        mock_bar.volume = 10000.0
        mock_bar.trade_count = 100
        
        mock_bar_qqq = MagicMock()
        mock_bar_qqq.timestamp = datetime(2026, 5, 18, 9, 30, tzinfo=timezone.utc)
        mock_bar_qqq.open = 99.95
        mock_bar_qqq.high = 100.15
        mock_bar_qqq.low = 99.75
        mock_bar_qqq.close = 100.0
        mock_bar_qqq.volume = 10000.0
        mock_bar_qqq.trade_count = 100
        
        mock_response = MagicMock()
        mock_response.data = {"SPY": [mock_bar], "QQQ": [mock_bar_qqq]}
        mock_client.get_stock_bars.return_value = mock_response
        
        mode_b_bars = fetch_alpaca_bars(
            symbol="SPY",
            start_time=datetime.now() - timedelta(days=1),
            end_time=datetime.now(),
            client=mock_client
        )
        self.assertEqual(len(mode_b_bars), 1)
        mode_b_bar = mode_b_bars[0]
        
        # 3. Perform strict side-by-side schema check
        self.assertEqual(set(mode_a_bar.keys()), set(mode_b_bar.keys()))
        
        for key in self.expected_keys:
            self.assertEqual(type(mode_a_bar[key]), type(mode_b_bar[key]))
            
        print("[!] Mode A and Mode B sample bars show 100% key and type swappability!")

if __name__ == "__main__":
    unittest.main()
