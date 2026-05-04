"""Configure pytest to find src/ modules."""
import sys
import os

# Add src/ to the path so test files can import from it
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
