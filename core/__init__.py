
"""Load deployment settings before any core module freezes its data paths."""
from env_bootstrap import load_environment

load_environment()
