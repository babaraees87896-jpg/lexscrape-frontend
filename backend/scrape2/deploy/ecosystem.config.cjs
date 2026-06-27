// PM2 config — path VPS pe /var/www/diaapi hona chahiye
module.exports = {
  apps: [
    {
      name: "diaapi",
      script: "./deploy/run_service.sh",
      cwd: "/var/www/diaapi",
      interpreter: "bash",
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: "512M",
      env: {
        NODE_ENV: "production",
        PORT: "8080",
        LOGIN_USER: "Demo9304",
        LOGIN_PASS: "Demo1234",
        USE_FLARESOLVERR: "1",
        FLARESOLVERR_URL: "http://127.0.0.1:8191/v1",
        CURL_IMPERSONATE: "chrome124",
      },
      error_file: "/var/www/diaapi/logs/pm2-error.log",
      out_file: "/var/www/diaapi/logs/pm2-out.log",
      merge_logs: true,
      time: true,
    },
  ],
};
