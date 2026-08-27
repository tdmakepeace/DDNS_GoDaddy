# GoDaddy Dynamic DNS agent

Keeps one or more GoDaddy **A records** (default `gateway`) pointed at this machine’s public IP using the [GoDaddy Domains API](https://developer.godaddy.com/doc/endpoint/domains#/v1/recordReplaceTypeName).

---

## Setup

### 1. Prerequisites

- Docker and Docker Compose
- DNS for the domain hosted on GoDaddy nameservers
- One or more **A** records to update (the agent replaces each named record; create them first if they do not exist)
- A GoDaddy **Production** API key and secret from [developer.godaddy.com/keys](https://developer.godaddy.com/keys)

### 2. Create GoDaddy API keys

1. Sign in at [developer.godaddy.com/keys](https://developer.godaddy.com/keys) with the GoDaddy account that owns the domain.
2. Go to Legacy API
3. Click **Create New API Key**.
4. Name it, for example `ddns-gateway`.
5. Choose **Production** / **Live**, not **OTE** / **Test**.
6. Copy **Key** and **Secret** immediately. The Secret is shown once.

Example of the create dialog (fake values):

```text
API Key created

Name:         ddns-gateway
Environment:  Production (Live)

Key:          3mMnyqKdZf_PqR2sW8tY6uI1oA5bC7dE8fG
Secret:       F9gH1iJ3kL5mN7oP9qR1sT3uV
```

| Field | Goes in config as |
| --- | --- |
| **Key** | `keys.api_key` |
| **Secret** | `keys.api_secret` |

### 3. Create the config file

Copy the example. `config/config.yaml` is gitignored and must not be committed.

```powershell
copy config\config.example.yaml config\config.yaml
```

```bash
cp config/config.example.yaml config/config.yaml
```

### 4. Fill in the A records and keys

Edit `config/config.yaml`:

```yaml
a_records:
  - gateway
  - vpn
domain: example.com
keys:
  api_key: 3mMnyqKdZf_PqR2sW8tY6uI1oA5bC7dE8fG
  api_secret: F9gH1iJ3kL5mN7oP9qR1sT3uV
ttl: 600
interval_seconds: 300
```

| Field | Required | Meaning |
| --- | --- | --- |
| `a_records` | yes | Hosts to update in each cycle. Default example is `gateway` (`gateway.example.com`). Use `@` for the apex. |
| `a_record` | no | Single-host form. Still accepted; merged with `a_records` if both are set. |
| `domain` | yes | Registered zone (for `.co.uk` set `example.co.uk`) |
| `keys.api_key` | yes | GoDaddy Production API key |
| `keys.api_secret` | yes | GoDaddy Production API secret |
| `ttl` | no | Record TTL in seconds (minimum **600**) |
| `interval_seconds` | no | How often to re-check the public IP (default `300`, minimum `30`) |
| `api_base` | no | Default `https://api.godaddy.com` |

Each host may be the label (`gateway`) or the FQDN (`gateway.example.com`). Duplicate labels are ignored. Keys may also sit at the top level as `api_key` / `api_secret`.

Optional environment overrides: `DDNS_A_RECORD`, `DDNS_DOMAIN`, `DDNS_API_KEY`, `DDNS_API_SECRET`. `DDNS_A_RECORD` may be a comma-separated list (`gateway,vpn,@`) and overrides YAML hosts.

### 5. Confirm the config

```bash
docker compose run --rm ddns --check-config
```

This prints the resolved hosts. It does not call the GoDaddy API. Secrets are not printed.

---

## Run

### Start

```bash
docker compose up -d --build
```

The container restarts unless you stop it. When the public IP changes, it updates every listed A record through the GoDaddy API.

### Logs

```bash
docker compose logs -f ddns
```

```text
A gateway.example.com already 203.0.113.10
Updating A gateway.example.com from 203.0.113.10 to 198.51.100.25
Updated A gateway.example.com -> 198.51.100.25
```

### One-shot update

```bash
docker compose run --rm ddns --once -v
```

### Stop / restart

```bash
docker compose restart ddns
docker compose down
```

Config is loaded at process start. After you edit `config/config.yaml`, restart the container.

---

## Edit

### Change the hostname or keys

1. Edit `config/config.yaml` (`a_records`, `domain`, `keys`).
2. Restart: `docker compose restart ddns`

### CLI flags

| Flag | Function |
| --- | --- |
| `-c`, `--config PATH` | Config file (default `/config/config.yaml` in the container) |
| `--once` | Discover IP, update DNS if needed, exit |
| `--check-config` | Parse config, print resolved records, exit (no API write) |
| `-v`, `--verbose` | Debug logging |

```bash
docker compose run --rm ddns --check-config
docker compose run --rm ddns --once -v
```

### Keep keys out of the file

Uncomment in `docker-compose.yml` and set the variables on the host:

```yaml
environment:
  DDNS_API_KEY: ${DDNS_API_KEY:-}
  DDNS_API_SECRET: ${DDNS_API_SECRET:-}
```

---

## Test locally with Python

Always create a local virtual environment first. Do not install packages into the system Python.

Unit tests do not call GoDaddy. `--once` does, and will update every listed live A record if the public IP has changed.

`.venv/` is gitignored.

### 1. Create and activate the local environment

From the repo root, **always** create `.venv` (safe to re-run if it already exists), then activate it and install deps.

**PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If script activation is blocked, skip `Activate.ps1` and call `.venv\Scripts\python.exe` in the commands below.

**bash**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The prompt should show `(.venv)`. Leave it active for the rest of this section.

### 2. Run the unit tests

```powershell
python -m unittest discover -s src -v
```

Without activation:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s src -v
```

```bash
.venv/bin/python -m unittest discover -s src -v
```

### 3. Validate config (no DNS write)

```powershell
python src/agent.py --config config/config.yaml --check-config
```

### 4. One-shot live update

```powershell
python src/agent.py --config config/config.yaml --once -v
```

Omit `--once` to run the same loop as the container (Ctrl+C to stop).

Deactivate when finished: `deactivate`.

---

## How an update works

1. Discover this host’s public IPv4.
2. For each configured host, `GET https://api.godaddy.com/v1/domains/{domain}/records/A/{name}` with `Authorization: sso-key {key}:{secret}`.
3. If that record already matches, leave it.
4. Otherwise `PUT` the same URL with `[{"data": "<ip>", "ttl": 600}]`.
5. If some records fail, the others are still attempted; the cycle fails if any host could not be updated.

HTTP `401` / `403` usually means an OTE key, a mistyped secret, or keys from an account that does not own the domain.
