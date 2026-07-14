# Legacy External GitHub Actions Scheduler

> **Retired:** Do not install or enable the systemd units documented in this
> file. The unconditional 30-minute dispatcher was replaced by the
> health-gated watchdog in `sebosimo/Hetzner_Server/github-actions-trigger`.

The legacy trigger sent a workflow dispatch every 30 minutes without checking
whether production was current. Its script and unit examples remain only as
rollback and implementation history.

The current production design is documented in
`README_coding_server_pipeline.md`:

1. The Coding Server publishes directly to Infomaniak as the primary.
2. The Hetzner weather server compares the live manifest with the latest
   profile-complete MeteoSwiss cycles every 30 minutes.
3. It dispatches GitHub Actions only after the same cycle is stale twice.
4. GitHub retains its six-hour native schedule as an independent last resort.

## Historical GitHub Setup

Create a fine-grained GitHub token for `sebosimo/XCBenz_Data_Parallel` with:

- Repository access: only `sebosimo/XCBenz_Data_Parallel`
- Repository permissions: `Actions` read/write

The classic PAT fallback also works for private repos, but it has broader
access than this trigger needs.

## Historical Server Setup

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

## Historical Behavior

- The retired timer fired at minute `00` and `30`.
- The service is `oneshot`, runs with low CPU and IO priority, and exits after
  the GitHub API accepts or rejects the dispatch.
- The trigger uses a non-blocking lock at
  `/run/lock/xcbenz-github-actions-trigger.lock` so duplicate local starts exit
  cleanly.
- The workflow uses `concurrency: cancel-in-progress: false`, so a new GitHub
  run will wait rather than interrupt a current data publish.
- The trigger passed `run_mode=standard-deploy-data-host`; the workflow's live
  preflight could turn an unnecessary dispatch into a cheap no-op.
- The dispatch uses the workflow's single `run_mode` input.
- The GitHub-hosted cron in `.github/workflows/daily_plot.yml` is reduced to a
  six-hour backup schedule so it does not compete with the server timer.

## Historical Operations

Useful commands:

```bash
systemctl list-timers xcbenz-github-actions-trigger.timer
journalctl -u xcbenz-github-actions-trigger.service --since today
systemctl status xcbenz-github-actions-trigger.timer
```

To ensure the retired scheduler is disabled on a host where it was previously
installed:

```bash
sudo systemctl disable --now xcbenz-github-actions-trigger.timer
```

Do not store the active watchdog token on the Coding Server. The active token
belongs only in the weather server's ignored
`github-actions-trigger/.env` file.
