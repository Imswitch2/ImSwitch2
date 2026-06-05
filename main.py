"""Local-dev launcher.

Runs the same entry point as the installed ``imswitch`` console script
from the IDE. The CLI form (``imswitch``) still defaults to INFO.
"""
import sys
import warnings

# Inject --debug unless the caller already passed it (or --no-debug, which
# argparse doesn't define — kept as an escape hatch via env var IMSWITCH_LOG_LEVEL).

warnings.filterwarnings("ignore", module="numpydoc")

from imswitch.__main__ import main
main()