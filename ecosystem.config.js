// pm2 config for termdeck.  Paths are derived from this file's location, so
// the repo can live anywhere:  pm2 start ecosystem.config.js
const path = require("path");

module.exports = {
  apps: [{
    name: "termdeck",
    script: path.join(__dirname, "server.py"),
    interpreter: path.join(__dirname, ".venv/bin/python"),
    cwd: __dirname,
    autorestart: true,
    restart_delay: 5000,
    // The server only exits when it can't bind port 7717, so a restart storm
    // means something else is holding it — stop trying and leave it in the log.
    max_restarts: 10,
    min_uptime: 10000,
    out_file: path.join(__dirname, "logs/termdeck.log"),
    error_file: path.join(__dirname, "logs/termdeck.err.log"),
    env: {
      // Python buffers stdout when it isn't a tty; without this `pm2 logs`
      // lags behind what the server is actually doing.
      PYTHONUNBUFFERED: "1",
      // pm2 resurrects with the environment saved at `pm2 save` time, which
      // may not include Homebrew — where the tailscale CLI usually lives.
      PATH: "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    },
  }],
};
