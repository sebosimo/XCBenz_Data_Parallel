# External GitHub Actions Scheduler

This is the preferred 30-minute scheduler for the XCBenz data pipeline. It
keeps timing on the Hetzner server and leaves GitHub Actions responsible only
for executing the workflow.

The script only sends one GitHub API request. It does not run the data pipeline
locally and does not need access to MeteoSwiss data, Infomaniak SSH keys, or
the generated `web_exports/` files.

## GitHub Setup

Create a fine-grained GitHub token for `sebosimo/XCBenz_Data_Parallel` with:

- Repository access: only `sebosimo/XCBenz_Data_Parallel`
- Repository permissions: `Actions` read/write

The classic PAT fallback also works for private repos, but it has broader
access than this trigger needs.

## Server Setup

Assuming the repo is checked out at `/opt/xcbenz/XCBenz_Data_Parallel` and the
service user is `xcbenz`:

```bash
sudo install -d -m 0750 -o root -g xcbenz /etc/xcbenz
sudo install -m 0600 -o root -g xcbenz deploy/github-actions-trigger.env.example /etc/xcbenz/github-actions-trigger.env
sudo editor /etc/xcbenz/github-actions-trigger.env

sudo install -m 0644 deploy/systemd/xcbenz-github-actions-trigger.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/xcbenz-github-actions-trigger.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now xcbenz-github-actions-trigger.timer
```

Before enabling the timer, test the payload locally:

```bash
python3 deploy/trigger_github_workflow.py --dry-run
```

Then test one real dispatch:

```bash
sudo systemctl start xcbenz-github-actions-trigger.service
sudo journalctl -u xcbenz-github-actions-trigger.service -n 50 --no-pager
```

## Behavior

- The timer fires at minute `00` and `30`.
- The service is `oneshot`, runs with low CPU and IO priority, and exits after
  the GitHub API accepts or rejects the dispatch.
- The trigger uses a non-blocking lock at
  `/run/lock/xcbenz-github-actions-trigger.lock` so duplicate local starts exit
  cleanly.
- The workflow uses `concurrency: cancel-in-progress: false`, so a new GitHub
  run will wait rather than interrupt a current data publish.
- The trigger passes `run_mode=standard-deploy-data-host`; unchanged MeteoSwiss
  cycles should finish as cheap no-op runs after preflight, while new cycles
  still deploy to `data.xcbenz.com`.
- The dispatch uses the workflow's single `run_mode` input.
- The GitHub-hosted cron in `.github/workflows/daily_plot.yml` is reduced to a
  six-hour backup schedule so it does not compete with the server timer.

## Operations

Useful commands:

```bash
systemctl list-timers xcbenz-github-actions-trigger.timer
journalctl -u xcbenz-github-actions-trigger.service --since today
systemctl status xcbenz-github-actions-trigger.timer
```

To pause external scheduling:

```bash
sudo systemctl disable --now xcbenz-github-actions-trigger.timer
```
