#!/usr/bin/env python3
"""users collection mein local admin + poori hierarchy."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mongodb.insert_hierarchy import insert_hierarchy

if __name__ == "__main__":
    insert_hierarchy()
