# -*- coding: utf-8 -*-
"""Pytest configuration for MOSAIC-Ω test suite."""
import os
import sys

# Ensure UTF-8 mode on Windows for pytest execution
os.environ["PYTHONUTF8"] = "1"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
