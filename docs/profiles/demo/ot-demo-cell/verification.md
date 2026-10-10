# OT demo cell: end-to-end verification

> **Audience:** operator · **Profile:** `demo` · **Read this when:** a cell is deployed and you want to prove every part of it works, layer by layer, before showing it to anyone.

Part of [OT demo cell](../ot-demo-cell.md).

## E2E verification checklist

Written for GCP (the first cloud); on AWS/Azure substitute that cloud's deploy child
(`ec2_deploy` / `azure_deploy`), destroy endpoint, functional account
(`passwordsafe_vm_functional_account_aws` / `_azure`), managed-system address shape
(`{instance-id}:{region}` / `tenantId/subscriptionId/resourceGroup/vmName`) and
gateway size key (see [the sizing guard](deploying.md#the-gateway-sizing-guard)). On AWS also
verify the cell landed in the **private** subnet (no public IP on the child job) and,
for Password Safe over SSM, that `aws_ssm_endpoints_enabled` is on or the subnet
otherwise reaches the SSM control plane. **As of 2026-08-25 the AWS and Azure slices
have not been E2E-verified live** — this checklist is the script for that pass.

The `ot-sim` image, the FUXA seed, the extra protocol sims and the Purdue rules have
**not been exercised on a live bake or cell** either: they are covered by unit and
structural tests, and the sims themselves were run and read against real clients
outside the image. The **k3s runtime is the newest of these**: the air-gapped install,
Docker staying beside it, the image side-load and the identity reset are held by
`tests/test_ot_k3s.py` and by the bake's own smoke test, and nothing more. Steps 2a, 4a
and 10 below are their first live pass; `OT_RUNTIME=docker` is the fallback if the bake
fails on a platform the script has not met.

1. Settings: `pra_enabled` on; `bt_api_host` / `bt_client_id` / `bt_client_secret` /
   `bt_jump_group_name` / `bt_jumpoint_name` set; `gcp_jumpoint_machine_type=e2-medium`
   (delete an existing gateway VM so it recreates); `gcp_vm_nat_enabled` **off**;
   Password Safe registration on with the GCP functional account.
2. Bake `ot-sim`; it appears in the OT tab's image picker. Watch the bake log for
   `installing k3s …`, CoreDNS becoming ready, every baked image found in containerd, the
   five ports answering the smoke test, and either `FUXA project seeded` or the
   `NOT seeded` warning.
   - **2a.** On the deployed cell (Shell Jump): `systemctl status ot-sim` is
     `active (exited)`, `kubectl -n ot-sim get pods` shows `ot-plc`, `ot-hmi`,
     `ot-opcua`, `ot-enip` and `ot-s7` **Running on the node's own IP** (`-o wide`),
     `docker ps` answers (Docker is there, the plant is not in it), and the Web Jump
     opens FUXA on a project that already has the
     `PLC` connection and its four tags. `kubectl get nodes` names *this* cell, not the
     build VM, and `kubectl -n kube-system get pods` is healthy with no image pulls
     pending — that is the air-gapped install doing its job.
3. Deploy a cell **with several protocols ticked** and the Jump Group + Gateway
   pickers set to the cell's region; the parent job completes; the child holds Shell
   Jump id + private IP; **one tunnel jump per ticked protocol** appears, each named
   `ot-<cell>-<protocol>`, all in the picked Jump Group (not the configured default),
   and each carrying an OT-cell comment rather than "k8s API tunnel". The cell shows
   **wired** only once every one of them exists.
4. PRA rep console: Shell Jump SSH works; Web Jump renders FUXA (recorded); a Modbus
   client (mbpoll / QModMaster) through the tunnel at `127.0.0.1:502` reads holding
   register 0 **incrementing every second**. Then deploy (or add a standalone tunnel
   for) each other vendor and confirm the same four values move:
   - Siemens — python-snap7 `db_read(1, 0, 8)` against `127.0.0.1:102`. The
     pure-Python server logs a COTP framing warning on some handshakes; the read is
     what matters, not that line;
   - Rockwell — `pylogix` `Read("Counter")` against `127.0.0.1:44818` (CIP tag
     names are case-sensitive);
   - OPC UA — UaExpert against `opc.tcp://127.0.0.1:4840`, browse `Objects/Plant`.

   `python scripts/ot/verify_tunnels.py` does all four in one pass and distinguishes
   "no listener" from "listener, no answer" from "answers but frozen" — see
   [OT protocol clients on Windows](ot-protocol-clients.md).
   - **4a. The cluster, through PRA.** With the cell deployed with **Kubernetes API
     (k3s)** ticked: the jump item `ot-<cell>-k3s` exists, and with it started,
     `kubectl --kubeconfig <the cell's /var/lib/ot-sim/kubeconfig-via-tunnel.yaml>
     -n ot-sim get pods` answers from the rep machine, with no certificate error
     (k3s's certificate names 127.0.0.1). Stop the jump and the same command fails to
     connect: the cluster has no other way in.
5. Password Safe: managed system `projectId/zone/instanceName` exists; the mirror
   system `<cell>-pravault` exists with account `<cell>-adminuser`; the pair shows
   under `adminuser`'s **Synced Accounts**; rotate `adminuser` and watch the change
   propagate to the mirror.
6. PRA: the Vault account `<cell>-adminuser` exists, is **check-out-able** in the rep
   console / `/login`, and is **offered for injection** when starting the cell's Shell
   Jump; after the rotation in step 5, checkout returns the NEW credential and SSH with
   it succeeds.
6a. **The plant's own agent.** Deploy a cell with **Register in Entitle** ticked
   and a broker image picked. The parent job shows: token minted → broker deployed →
   cell deployed → PRA wiring → both zones applied → agent install queued.
   The zones are readable from the cloud's own CLI — `gcloud compute firewall-rules
   list`, `aws ec2 describe-security-groups --group-names <cell>-ot-zone
   <broker>-dmz-zone`, or `az network nsg rule list -g <rg> --nsg-name <cell>-ot-zone`
   — and on AWS confirm the cell's *instance* carries `<cell>-ot-zone` and nothing
   else, since a zone beside a permissive group restricts nothing. On Azure the row
   to look for is the **outbound Deny**: without it the cell reaches the internet
   despite having no public IP.
   `gcloud compute firewall-rules list` shows the two zones from
   [Who brokers identity in the plant](identity.md#who-brokers-identity-in-the-plant). From the
   broker (Shell Jump): `kubectl -n entitle get pods` is Running and the install job's
   probe passed. From the cell: every `curl` fails. Then request access in Entitle and
   confirm the ephemeral account appears **on the cell** — that is the agent reaching it
   from inside the plant — and that SSH with it through the Shell Jump works and expires
   on its own. Destroy: both VMs, both rule sets, the agent token and the integration go.
7. Negative test: set the gateway to `e2-micro` → a new cell fails fast with the sizing
   remedy in the job error (not a mid-session OOM). With a Gateway override picked, the
   same deploy proceeds (guard skipped, noted in progress).
8. **Per-tunnel Re-wire**: delete one of the cell's tunnel jumps in PRA, press
   **Re-wire**, and confirm only that protocol is recreated (the others are untouched
   and not duplicated). Re-wire again with nothing missing and confirm it completes
   without provisioning anything.
9. Destroy the cell → **every** tunnel jump gone from PRA (check each protocol, not
   just the first), the Web Jump gone, the Vault checkout account gone, the mirror + PS
   system off-boarded, VM deleted, gateway reaped only once nothing else references it.
10. **Back-compat**: destroy a cell deployed *before* this change — its metadata has the
    singular `ot_tunnel_*` keys and no `ot_tunnels` list — and confirm its tunnel is
    still torn down. (A cell in that state also Re-wires cleanly: the existing tunnel is
    adopted into the list rather than provisioned a second time.)
11. Expiry: with the timer enabled, `expires_at` is stamped on the child row; a reaped
    cell cleans up identically to a destroyed one — including every tunnel.
12. **Purdue rules (GCP, optional)**: with `ot_purdue_firewall_enabled` on, deploy a
    cell (or **Re-wire** an existing one) and check `gcloud compute firewall-rules list`
    shows its three `<cell>-ot-*` rules. Then: Shell Jump, Web Jump and the tunnel all
    still work; `curl` to the internet from the cell fails **even with
    `gcp_vm_nat_enabled` on**; and a destroy removes all three rules.
13. **Standalone tunnels still work alongside**: the cell's own tunnels cover the
    protocols it was deployed with, so use a standalone tunnel for what it was not —
    e.g. DNP3 to real gear, or a second local port for a protocol already brokered.
    Two tunnels cannot listen on the same local port on one rep machine at once.
