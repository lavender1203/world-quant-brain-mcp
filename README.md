# world-quant-brain-mcp

## Setup

1. Install dependencies:

```bash
python 配置前运行我_安装必要依赖包.py
```

2. Configure environment variables (recommended):

- Copy `.env.example` to `.env` and set your values. The server will automatically load these.
- Required:
	- `CREDENTIALS_EMAIL`, `CREDENTIALS_PASSWORD`
- Optional:
	- `API_SETTINGS_TIMEOUT` (seconds, default 30)
	- `FORUM_SETTINGS_BASE_URL` (default https://support.worldquantbrain.com)
	- `FORUM_SETTINGS_HEADLESS` (true/false, default true)
	- `FORUM_SETTINGS_TIMEOUT` (seconds, default 15)
	- `MCP_HOST` (default 0.0.0.0 for remote SSE)
	- `MCP_PORT` (default 8000)
	- `MCP_SSE_PATH` (default /sse)

The server still supports `user_config.json`, but `.env` values take precedence for overlapping keys.

3. Run the MCP server as described below.

## Run (SSE)

From the project root with the virtual environment activated:

```bash
./.venv/bin/mcp run --transport sse main.py:mcp
```

The SSE endpoint will listen on `http://$MCP_HOST:$MCP_PORT$MCP_SSE_PATH` (defaults: 0.0.0.0:8000/sse). Use a reverse proxy with HTTPS for production.

## Detailed Install (recommended)

1. Create and activate a Python virtual environment (if you haven't):

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Upgrade pip and install project dependencies using the provided installer script:

```bash
pip install -U pip
python 配置前运行我_安装必要依赖包.py
```

3. (Optional but required for forum/playwright tools) Install Playwright and browser engines:

```bash
pip install playwright
python -m playwright install chromium
```

4. Start the SSE MCP server:

```bash
./.venv/bin/mcp run --transport sse main.py:mcp
```

Notes:
- If your environment restricts installing system packages, ask your admin to preinstall `python3-venv` and `python3-pip`.
- For production, place nginx/caddy (HTTPS) in front of the MCP SSE endpoint and run the MCP process under `systemd` for reliability.

## Nginx reverse-proxy example

Place the example config at `/etc/nginx/sites-available/mcp_sse` and symlink to `sites-enabled`.
Example file in this repo: `deploy/nginx/mcp_sse.conf`.

Key points:
- Use `proxy_http_version 1.1` and `proxy_set_header Connection ""` to allow long-lived SSE connections.
- Disable buffering with `proxy_buffering off` so events flow immediately.
- Put TLS (Let's Encrypt / certbot) in front of nginx or configure nginx with your certificates.

Reload nginx after enabling the site:

```bash
sudo ln -s /etc/nginx/sites-available/mcp_sse /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

## systemd service example

A unit file is included at `deploy/systemd/mcp-sse.service` — copy it to `/etc/systemd/system/` and enable:

```bash
sudo cp deploy/systemd/mcp-sse.service /etc/systemd/system/mcp-sse.service
sudo systemctl daemon-reload
sudo systemctl enable --now mcp-sse.service
sudo journalctl -u mcp-sse -f
```

The unit uses `/opt/project/world-quant-brain-mcp/.env` as `EnvironmentFile` and runs the `mcp` CLI from the virtualenv. Adjust paths, `User`, and permissions to suit your environment.