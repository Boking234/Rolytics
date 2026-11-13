# Security

- Your Roblox API key is stored only on your computer, in `~/.roblox-agent/secrets.env`
  (outside this project folder, readable only by your user account). It is never printed,
  logged, saved in the database, or uploaded anywhere.
- Give the key **only** the `universe.analytics:read` permission, and only for your own game.
- If you think a key leaked, delete it on the Creator Dashboard (Credentials page), create a
  new one, and run `roblox-agent setup` again. `roblox-agent forget-key` removes the saved copy.
- Found a security problem in this tool? Please open a private security advisory on GitHub
  instead of a public issue.
