"""``python -m agent.worker``: the production worker (ADR-014 Part 1)."""

import os
import sys

from agent.worker.app import production_main

if __name__ == "__main__":
    sys.exit(production_main(os.environ))
