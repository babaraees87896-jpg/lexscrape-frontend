/**
 * PM2 — 1ex.in backend stack
 * Run from APP_DIR: pm2 start deploy/digitalocean/ecosystem.config.cjs
 */
const path = require("path");
const fs = require("fs");

const APP = process.env.APP_DIR || "/opt/1ex";
const envPath = path.join(APP, ".env");
const env = {};

if (fs.existsSync(envPath)) {
  for (const line of fs.readFileSync(envPath, "utf8").split("\n")) {
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;
    const i = t.indexOf("=");
    if (i < 1) continue;
    env[t.slice(0, i).trim()] = t.slice(i + 1).trim();
  }
}

const base = {
  EX99_MONGO_URI: env.EX99_MONGO_URI || "mongodb://127.0.0.1:27017",
  EX99_MONGO_DB: env.EX99_MONGO_DB || "ex99_local",
  PYTHONUNBUFFERED: "1",
  PYTHONIOENCODING: "utf-8",
};

module.exports = {
  apps: [
    {
      name: "1ex-main",
      cwd: path.join(APP, "backend"),
      script: "serve_local.py",
      interpreter: path.join(APP, ".venv/bin/python"),
      env: {
        ...base,
        EX99_PORT: env.PORT_MAIN || "1456",
        EX99_HOST: env.EX99_HOST || "1ex.in",
        EX99_LOCAL_ONLY: env.EX99_LOCAL_ONLY || "1",
        EX99_ADMIN_UPSTREAM_HOST: "127.0.0.1",
        EX99_ADMIN_UPSTREAM_PORT: env.PORT_ADMIN || "1457",
        EX99_AUTO_DECISION: env.EX99_AUTO_DECISION || "1",
        EX99_SCORECARD_LIVE_HUB: env.EX99_SCORECARD_LIVE_HUB || "1",
        EX99_SCORECARD_PREWARM: env.EX99_SCORECARD_PREWARM || "1",
        EX99_MAX_HTTP_THREADS: env.EX99_MAX_HTTP_THREADS || "200",
      },
      max_restarts: 20,
      autorestart: true,
    },
    {
      name: "1ex-admin",
      cwd: path.join(APP, "backend"),
      script: "serve_admin.py",
      interpreter: path.join(APP, ".venv/bin/python"),
      env: {
        ...base,
        EX99_ADMIN_PORT: env.PORT_ADMIN || "1457",
        EX99_ADMIN_HOST: env.EX99_ADMIN_HOST || "admin.1ex.in",
      },
      max_restarts: 20,
      autorestart: true,
    },
    {
      name: "1ex-center",
      cwd: path.join(APP, "backend"),
      script: "serve_centerpanel.py",
      interpreter: path.join(APP, ".venv/bin/python"),
      env: {
        ...base,
        EX99_CENTERPANEL_PORT: env.PORT_CENTER || "1458",
        EX99_CENTERPANEL_HOST: env.EX99_CENTERPANEL_HOST || "center.1ex.in",
      },
      max_restarts: 20,
      autorestart: true,
    },
    {
      name: "1ex-staff",
      cwd: path.join(APP, "backend/bluewin"),
      script: "serve_bluewin.py",
      interpreter: path.join(APP, ".venv/bin/python"),
      env: {
        ...base,
        BLUEWIN_PORT: env.PORT_STAFF || "1460",
        BLUEWIN_PUBLIC_HOST: env.STAFF_HOST || "staff.1ex.in",
        STAFF_HOST: env.STAFF_HOST || "staff.1ex.in",
        WNP9_USERNAME: env.WNP9_USERNAME || "",
        WNP9_PASSWORD: env.WNP9_PASSWORD || "",
        WNP9_API_BASE: env.WNP9_API_BASE || "https://api.wnp9.pro/v1/",
      },
      max_restarts: 20,
      autorestart: true,
    },
  ],
};
