# Telegram Subscription Collector

This repository reads the public Telegram post at `https://t.me/xsfilterrnet/3549`, extracts its external HTTP(S) links, downloads their textual bodies, and commits a merged snapshot five times per Tehran day.

## Generated files

- `subscriptions/all.txt` — all usable source bodies concatenated in message order.
- `subscriptions/source-urls.txt` — canonical, deduplicated source URLs.
- `subscriptions/manifest.json` — per-source status, size, SHA-256, content type, and errors.
- `subscriptions/sources/*.txt` — last successful body for each URL, used as a cache during temporary outages.

## Install

1. Copy the complete repository contents into a GitHub repository.
2. Commit and push to the repository's **default branch**.
3. Open **Actions → Update Telegram subscriptions → Run workflow** for the first test.
4. Confirm that the workflow can push. The workflow requests only `contents: write`; if an organization policy prevents it, allow write access for the repository's `GITHUB_TOKEN` in **Settings → Actions → General**.

No Telegram bot token, account login, PAT, or repository secret is required for a public repository/post.

## Schedule

The workflow runs at `00:17`, `05:17`, `10:17`, `15:17`, and `20:17` in the `Asia/Tehran` timezone. The unusual minute avoids GitHub's busiest start-of-hour queue period.

## Reliability and safety behavior

- The live Telegram post is the primary URL source.
- If Telegram is unavailable, the previous successful URL list is reused.
- On a brand-new repository, `config/fallback_urls.txt` is the final fallback.
- If an individual source fails, its last successful cached body remains in `all.txt` and is marked `stale-cache`.
- If a source has never succeeded, it is marked `failed-no-cache` and omitted from the merged body.
- Private, loopback, link-local, reserved, and other non-global destinations are rejected, including after redirects.
- Response and aggregate size limits prevent unexpectedly large downloads.
- Downloaded text is never executed.
- A commit is created only when generated files actually change.

## Important format limitation

The Telegram message mixes plain VLESS/VMess lists, Base64 subscriptions, Clash/Mihomo YAML, HTML, and other formats. `subscriptions/all.txt` is therefore an archive-style concatenation and is **not guaranteed to be directly importable by one VPN client**. The original per-source files remain available under `subscriptions/sources/`.

## Local test

```bash
python -m unittest discover -s tests -v
python scripts/collect_subscriptions.py \
  --message-url "https://t.me/xsfilterrnet/3549" \
  --fallback-file "config/fallback_urls.txt" \
  --output-dir "subscriptions" \
  --summary-file "/tmp/collector-summary.md"
```
