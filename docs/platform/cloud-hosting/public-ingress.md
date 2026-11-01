# Cloud Hosting: Public Ingress Through a Tunnel

> **Audience:** operator · **Profile:** `both` · **Read this when:** your dashboard runs on Docker Compose behind NAT, and remote agents (or a cloud STS) need to reach it with no inbound ports opened.

Part of [Cloud Hosting](../cloud-hosting.md).

Remote agents dial in to `/api/agent/*`. When the dashboard uses its own
[workload identity](../workload-identity.md), the clouds fetch its issuer documents from
`/spiffe/*`. Both need a public HTTPS hostname. `docker-compose.agent.yml` provides one
by publishing 80/443 and getting a Let's Encrypt certificate, which needs inbound
reachability. An **outbound tunnel** doesn't: the connector dials out to the provider's
edge, which terminates TLS and forwards requests back down the tunnel.

Two providers are supported, and they share everything except the connector container.

| | Cloudflare Tunnel | ngrok |
|---|---|---|
| Best for | Installs that stay up | Demos, POV and dev: running in minutes |
| You need | A domain whose DNS is on Cloudflare | An ngrok account (authtoken + one static domain) |
| Cost | Free, including Access for ≤ 50 users | Free tier covers the agent endpoint; custom domains, IP rules and OAuth at the edge are paid |
| Publishing the UI too | Yes: a second hostname behind Cloudflare Access | Not supported by this overlay (one endpoint) |
| Watch out for | Never put Access on the agent hostname, because agents can't answer a login redirect | Free tier shows browsers an interstitial page (agents are unaffected). Some corporate web filters block `*.ngrok-free.app`. |

**Recommendation:** use ngrok to get an agent enrolled this afternoon, and Cloudflare
Tunnel with Access for anything that stays up.

---

## The shape

```
agent ──HTTPS──► provider edge ══tunnel══► connector ─► 127.0.0.1:80 Caddy ─► 127.0.0.1:8000 app
                 (TLS ends here)           └──────── all in the app container's network namespace ────────┘
```

- **The gateway is the path filter**, not the provider. `docker-compose.ingress.yml` runs
  [`examples/cloud-gateway/Caddyfile`](../../../examples/cloud-gateway/Caddyfile), the same
  file the managed-runtime sidecar uses.
  - `AGENT_HOSTNAME` serves `/api/agent/*` and `/spiffe/*`. Every other path gets one
    identical 404.
  - The UI is served only on `UI_HOSTNAME`, which is unset by default.
  - `tests/test_agent_vhost_surface.py` checks that file against the app's real route
    table, so this path can't publish an operator route.
- **Everything is loopback.** The gateway and the connector use
  `network_mode: "service:app"`. The app trusts `X-Forwarded-For` only from
  `TRUSTED_PROXY_HOSTS`, whose default `127.0.0.1` is already correct. There's no
  static subnet or literal gateway IP to maintain, unlike `docker-compose.agent.yml`.
- **`PUBLIC_BASE_URL` is set to `https://$AGENT_HOSTNAME`** by the overlay. Agents check
  that value as the signing audience, so it has to be the tunnel hostname.

## Set up: ngrok

1. In dashboard.ngrok.com, copy your **authtoken** and claim a **static domain**. The
   free plan includes one. A random per-restart URL would orphan every enrolled agent.
2. In `.env`:
   ```ini
   AGENT_HOSTNAME=your-name.ngrok-free.app
   NGROK_AUTHTOKEN=...
   ```
3. Start it:
   ```powershell
   .\scripts\Onboard-Dashboard.ps1 -Hub -Ingress ngrok
   ```
   ```bash
   ./scripts/onboard.sh --hub --ingress=ngrok
   ```

## Set up: Cloudflare Tunnel

1. Go to **Zero Trust → Networks → Tunnels → Create a tunnel** (type *cloudflared*) and
   copy its token.
2. Add a **public hostname** for the agents:
   - Hostname: your `AGENT_HOSTNAME`, e.g. `agents.example.com`.
   - Service: `HTTP` → `localhost:80`.

   `localhost` is correct, because the connector shares the app's network namespace and
   port 80 there is the gateway. Don't point it at `:8000`: that skips the path filter
   and publishes the whole app.
3. *Optional, to reach the UI from outside:*
   - Create an **Access application** for e.g. `dashboard.example.com` first.
   - Then add it as a second public hostname with the same service.
   - Set it as `UI_HOSTNAME`. The dashboard's own login and SSO still apply behind
     Access.
4. In `.env`:
   ```ini
   AGENT_HOSTNAME=agents.example.com
   CLOUDFLARE_TUNNEL_TOKEN=...
   #UI_HOSTNAME=dashboard.example.com
   ```
5. Run the onboarding script with `-Ingress cloudflare` (PowerShell) or
   `--ingress=cloudflare` (bash).

Without the scripts, stack the files yourself. Use `docker-compose.hub.yml` as the base
if you run the published image:

```bash
docker compose -f docker-compose.yml -f docker-compose.ingress.yml \
               -f docker-compose.ngrok.yml up -d      # or docker-compose.cloudflared.yml
```

Use the **same file list** for `down`, `logs` and later `up -d`. The connector and the
gateway live in the app's namespace, so they have to be recreated whenever the app is.

## Check it

From anywhere outside your network:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://$AGENT_HOSTNAME/              # 404
curl -s -o /dev/null -w '%{http_code}\n' https://$AGENT_HOSTNAME/setup         # 404
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://$AGENT_HOSTNAME/api/agent/lease   # 401: reachable, signature required
```

Then enrol an agent against `https://$AGENT_HOSTNAME`
([remote agents](../spiffe/remote-agents.md)).

If the app logs `Ignoring X-Forwarded-* from <peer>`, something isn't on loopback. Most
likely the provider is pointed at a different address than `localhost:80`.

## What this does not cover

- **Inbound webhooks** such as Entitle's custom-integration callbacks
  (`/api/entitle/rest/*`) are **not** published. The agent vhost's route set is pinned
  on purpose. Adding a prefix means changing `examples/cloud-gateway/Caddyfile` and its
  test together, and that changes the surface of every gateway that uses the file.
- **The local `:8001` port** is still published by the base compose file, on the LAN
  only. The tunnel doesn't expose it.
