"""CI stand-in for a broken release: starts, fails like the real bot does, exits."""
import logging
import sys

logging.basicConfig(level=logging.INFO)
logging.getLogger("mp3bot").error("fatal error (CI stub of a broken release)")
sys.exit(1)
