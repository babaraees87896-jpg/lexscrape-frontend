/**
 * PM2 — live data pollers (scrape2)
 * Start: APP_DIR=/opt/lex pm2 start deploy/digitalocean/ecosystem.live-poll.config.cjs
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

const py = path.join(APP, ".venv/bin/python");
const scrapeCwd = path.join(APP, "backend/scrape2");
const logs = path.join(APP, "logs");

const pollEnv = {
  PYTHONUNBUFFERED: "1",
  PYTHONIOENCODING: "utf-8",
  LOGIN_USER: env.LOGIN_USER || "Demo9304",
  LOGIN_PASS: env.LOGIN_PASS || "Demo1234",
  SPORT_POLL_INTERVAL: env.SPORT_POLL_INTERVAL || "30",
  SPORT_MATCH_DETAIL: env.SPORT_MATCH_DETAIL || "1",
  SPORT_MATCH_ALL: env.SPORT_MATCH_ALL || "0",
  SPORT_SCORECARD: env.SPORT_SCORECARD || "0",
  SPORT_DELAY: env.SPORT_DELAY || "0.15",
  COOKIE_REFRESH_SEC: env.COOKIE_REFRESH_SEC || "900",
  USE_FLARESOLVERR: env.USE_FLARESOLVERR || "1",
  FLARESOLVERR_URL: env.FLARESOLVERR_URL || "http://127.0.0.1:8191/v1",
  POLL_INTERVAL: env.CASINO_POLL_INTERVAL || "5",
  GAME_DELAY: env.CASINO_GAME_DELAY || "0.12",
};

const apps = [];

if (env.EX99_ENABLE_SPORT_POLL !== "0") {
  apps.push({
    name: "lex-sport-poll",
    cwd: scrapeCwd,
    script: "poll_sport.py",
    interpreter: py,
    env: pollEnv,
    error_file: path.join(logs, "sport-poll.err.log"),
    out_file: path.join(logs, "sport-poll.out.log"),
    max_restarts: 30,
    autorestart: true,
    restart_delay: 15000,
  });
}

if (env.EX99_ENABLE_CASINO_POLL === "1") {
  apps.push({
    name: "lex-casino-poll",
    cwd: scrapeCwd,
    script: "poll.py",
    interpreter: py,
    env: pollEnv,
    error_file: path.join(logs, "casino-poll.err.log"),
    out_file: path.join(logs, "casino-poll.out.log"),
    max_restarts: 30,
    autorestart: true,
    restart_delay: 15000,
  });
}

module.exports = { apps };
