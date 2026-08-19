"""Put the project root on sys.path so tests can import config/game/bank/cards."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
