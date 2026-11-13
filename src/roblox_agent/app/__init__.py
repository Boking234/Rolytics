"""The desktop app: first-run setup, today's check-up, an inbox, and settings.

It is a small local web page shown in its own window (pywebview) or, if that
isn't installed, in your browser. The page talks only to a server on
127.0.0.1 that this app starts, protected by a random token made at launch.
Nothing here needs the internet except Roblox itself.
"""
