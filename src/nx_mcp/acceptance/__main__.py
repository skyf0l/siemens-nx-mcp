"""``python -m nx_mcp.acceptance run|verify|call|fold`` (see :mod:`nx_mcp.acceptance.runner`)."""

import sys

from nx_mcp.acceptance.runner import main

sys.exit(main())
