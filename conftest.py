"""Put the repo root on sys.path so tests under tests/ can import the
top-level modules (relevance, qfc_coupon_clipper) no matter where pytest
is invoked from."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
