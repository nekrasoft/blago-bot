# OpenAI proxy on cv8282683

The bot uses `OPENAI_PROXY_URL=http://127.0.0.1:18080` in its `.env`.
Only the OpenAI SDK client uses this proxy. There is no direct fallback.

Xray is installed at `/usr/local/bin/xray` from the official XTLS/Xray-core
release v26.10.31; the release archive SHA256 was verified before installation.
`xray-openai.service` runs as the dedicated unprivileged `xray-openai` user.
The configuration is `/etc/blago/xray/config.json` (root:xray-openai, 640).

Secrets are outside Git in `/etc/blago/openai-proxy.env` (root:root, 600):

```dotenv
VPN_SUBSCRIPTION_URL=https://provider.example/SECRET
VPN_PROFILE_NAME=exact profile remarks from the subscription
```

The updater supports the provider's native JSON array and one selected
VLESS/TCP/REALITY profile. It ignores subscription listeners, routing and direct
outbounds. A changed candidate must pass Xray validation and an authenticated
OpenAI `/v1/models` request on a temporary local proxy before replacing the live
configuration. Failed validation leaves the live configuration untouched.
If the proxy cannot start after replacement, the previous configuration is restored.
The timer checks the subscription daily at 05:15 server time, with up to 15 minutes
of random delay. An unchanged configuration does not restart Xray.

Installed unit files are copied from this directory to `/etc/systemd/system/`.
`bot-proxy.conf` is installed as `/etc/systemd/system/bot.service.d/openai-proxy.conf`
to start Xray before the bot at boot, without stopping the bot when Xray restarts.
For initial setup, create the `xray-openai` system user and the config directory
(root:xray-openai, 750). `/etc/blago` must permit traversal by that user without
allowing it to list or read subscription secrets (root:root, 711).

```bash
# Bot update
cd /root/blago/blago-bot
git pull --ff-only origin main
systemctl restart bot.service

# Subscription refresh / diagnostics (no secret URLs in command arguments)
systemctl start openai-proxy-update.service
systemctl status xray-openai.service openai-proxy-update.timer
journalctl -u openai-proxy-update.service --no-pager -n 20
```

The updater calls the existing virtual environment Python and reads the bot's
existing API key for its connectivity probe. It does not send documents or invoke
paid generation during subscription checks. Failed refreshes log only the error
class to avoid leaking URLs or credentials. Inspect secret files privately when
investigating such failures.

## Automatic recovery

`openai-proxy-health.timer` runs the updater with `--health` two minutes after
the previous check finishes. A healthy proxy requires no subscription download
or restart. After two failed OpenAI `/v1/models` probes, it fetches the subscription
and tests the selected node, then other compatible VLESS/TCP/REALITY nodes in order.
Only a candidate passing an authenticated OpenAI probe is installed. The live
port is checked after restart; failure restores the previous configuration.
The selected profile is saved in the private environment file for future updates.
HTTP 401 and 429 stop recovery without switching nodes. Other HTTP failures and
network errors trigger recovery; if all candidates fail, the live config stays.
A shared file lock prevents concurrent daily updates and recovery runs.

```bash
cp deploy/openai-proxy/openai-proxy-{health.service,health.timer,update.service} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now openai-proxy-health.timer
systemctl start openai-proxy-health.service
journalctl -u openai-proxy-health.service -n 20 --no-pager
```

Recovery does not replay failed document batches: resend them after restoration.
