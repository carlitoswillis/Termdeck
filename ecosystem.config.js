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
    // `git pull` doesn't restart anything on its own — the running process
    // already has the old code in memory. Watching server.py means a pull
    // that changes it restarts termdeck; static/ needs no restart because
    // those files are read from disk per request.
    watch: [path.join(__dirname, "server.py")],
    ignore_watch: ["logs", ".git", ".venv", "tests", "static"],
    // Never stop trying. This is how you reach the machine; pm2 giving up
    // permanently means the only way back in is a remote desktop session.
    max_restarts: 10000,
    min_uptime: 10000,
    exp_backoff_restart_delay: 200,
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
