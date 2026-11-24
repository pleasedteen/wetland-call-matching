#!/usr/bin/env python3
"""启动一座模拟录音站。用法: python run_station.py --station A --port 8101"""
import sys

from birdsurvey.station import main

if __name__ == "__main__":
    sys.exit(main())
