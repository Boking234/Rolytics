"""Entry point for the packaged Windows app (RobloxAnalyticsAgent.exe, no console window).

Double-click: the app. Also accepts ``mcp`` so an agent pointed at this file still works.
"""
import sys

if len(sys.argv) > 1 and sys.argv[1] == "mcp":
    from roblox_agent.cli import main
    sys.exit(main(["mcp"]))

from roblox_agent.app.main import main

sys.exit(main())
