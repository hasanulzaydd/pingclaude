"""Double-click to open the PingClaude settings window (Windows)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pingclaude_gui

sys.exit(pingclaude_gui.main())
