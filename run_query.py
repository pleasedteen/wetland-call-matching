#!/usr/bin/env python3
"""查询入口，如: python run_query.py pairs --start ... --end ..."""
import sys

from birdsurvey.query import main

if __name__ == "__main__":
    sys.exit(main())
