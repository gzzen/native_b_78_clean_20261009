#!/usr/bin/env python3
# Path: conditions/b/network_probe.py
# Purpose: Launch the shared isolated OpenCode science diagnostic for Condition B.
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common.integrity.network_probe_conditions import main


if __name__ == "__main__":
    main(default_condition="b")
