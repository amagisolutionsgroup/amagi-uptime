# amagi-uptime

Off-PC uptime checks for LedgerSpeak and Amagi Solutions Group. GitHub Actions runs them every 5 minutes. The repo is public, so Actions minutes are free.

Checks:
- `https://app.amagisolutionsgroup.com/api/health`: app and database
- the cron heartbeat from that endpoint: `scheduledJobs` (covers `intuit-meter-check`) plus `lastProbeAt` under 20 minutes old
- `https://getledgerspeak.com/`
- `https://amagisolutionsgroup.com/`

A check must fail twice in a row before a DOWN alert goes to Telegram and email. While it stays down, the alert repeats every 6 hours. One RECOVERED message goes out when it passes again. GitHub can start scheduled runs late, so expect detection in roughly 5 to 15 minutes.

Manual test: Actions > uptime > Run workflow > test_alert = true.
