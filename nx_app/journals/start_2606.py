# nx: threaded
"""Same as start.py, which starts NX MCP on any NX build; kept for existing shortcuts."""

import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().with_name("start.py")))
