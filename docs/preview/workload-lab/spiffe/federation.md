# Federating a SPIRE lab with the dashboard

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want the lab's trust domain federated with the dashboard's own, and to prove its workloads across it.

Part of [SPIFFE and SPIRE](../spiffe.md).

**Preview, every deployment mode.** A lab's trust domain can be federated with the
dashboard's own SPIRE server ([the dashboard's SPIFFE identity](../../../platform/oidc/dashboard-identity.md)),
two trust domains that each trust the other. That is the setup customers who run more than
one trust domain ask about. Press **Federate** on an available lab's row.

Each SPIRE server serves its trust bundle on **tcp/8082** from a SPIFFE bundle endpoint
(`https_spiffe`, which is TLS with the server's own SVID). Each holds a federation
relationship naming the other's endpoint, so each fetches the other's bundle and keeps it
current through key rotation on its own. The job runs in this order:

1. **The lab's half.** It re-applies the lab's install playbook, which adds the bundle
   endpoint to a lab built before this feature and restarts its server. It opens tcp/8082
   on the lab's ACL and host firewall to the lab's existing sources, then runs
   `spire-federation.yml`. That playbook creates the relationship, seeded with the
   dashboard's current bundle, and refreshes it once to prove the lab can fetch.
2. **The dashboard's half.** It creates the mirror relationship on the dashboard's SPIRE
   server, seeded with the bundle **Refresh keys** captured, and refreshes it once.

It needs both directions open:

| From | To | Port | Opened by |
|---|---|---|---|
| the lab host | the dashboard's SPIRE host | tcp/8082 | you: `docker-compose.spire.yml` publishes it (`SPIRE_FEDERATION_PORT`); firewall it to the lab hosts |
| the dashboard host | the lab host | tcp/8082 | the lab's ACL, to its existing sources, which must include the dashboard's egress, as they already do for 8443 |

A failed refresh names the direction that is closed. **Unfederate** (the **Federated ✓**
button) removes both relationships and both federated bundles. Decommissioning a lab
removes the dashboard's relationship with it.

The playbook is `spire-federation.yml`. Upload it with the rest of the `spire-*.yml` set on
the Storage page. Bundles are public keys, so nothing secret moves in either direction.

## Helm-mode (k8s) labs

The same button and the same job. Three things differ:

- **The bundle endpoint.** `spire-helm.yml` turns on the chart's `spire-server.federation`
  (an `https_spiffe` bundle endpoint on 8082) and publishes it on the lab host's tcp/8082
  with a third ServiceLB Service, `spire-federation-lab`, beside the API and OIDC ones.
  The play reads the rendered server config back and fails, naming the fix, if the chart
  did not take the values. Federating re-applies this install (a `helm upgrade`), so a
  k8s lab built before this gets the endpoint then.
- **The relationship is declared, not created.** The chart's controller manager
  reconciles federation relationships from `ClusterFederatedTrustDomain` resources and
  deletes any relationship no resource declares, within about 10 seconds. A CLI
  `federation create` would be undone, so `spire-federation.yml` applies a
  `ClusterFederatedTrustDomain` instead, with the controller manager's own `className`
  read from its ConfigMap (the chart ignores a resource without it). It then waits for the
  server to report the relationship and refreshes it, as in the other modes. The seed
  bundle is used once, at creation. The manager compares only the URL and profile, so it
  never overwrites a bundle SPIRE has refreshed since.
- **Unfederate** deletes the resource, waits for the manager to remove the relationship,
  then deletes the federated bundle. Decommissioning deletes the lab's resources and its
  `spire-federation-lab` Service before uninstalling the charts.

The workload's `federatesWith` is still written through the CLI: that entry belongs to the
k3s link, not to the controller manager, which leaves entries it did not create alone.

## The workloads, and the proof

A relationship makes a **server** hold the other trust domain's bundle. A registration
entry's `federatesWith` is what hands that bundle to a **workload** in its SVID response,
which is what lets the workload authenticate a peer from the other trust domain over mTLS.
**Federate** sets it on both sides:

- **The lab's k8s workload** (`deploy-bot`, when Kubernetes is linked) gets the
  dashboard's trust domain. An existing entry is updated by `spire-federation.yml`, and a
  link made after federating creates the entry that way (`spire-k8s-entry.yml`).
- **Agent cells on the dashboard's trust domain** get the lab's trust domain. Existing
  cells are updated on the dashboard's server, and a cell minted later federates with
  every federated lab from the start. Revoked cells are left alone.

**The proof** runs on the linked k3s node, as the workload: `spire-federation-proof.yml`
asks the agent's Workload API for the workload's X.509-SVID response and checks that
`spiffe://<dashboard trust domain>` is among its federated bundles. The row's **Federated ✓**
shows the result. That response carries the workload's private key, so the fetch is never
logged and the play reports trust domain names only.

The dashboard does not run playbooks on an agent cell's host, so prove that side by hand,
on the cell's host, as the cell's worker user:

```bash
ansible-playbook -i <cell host>, examples/playbooks/spire/spire-federation-proof.yml \
  -e federated_trust_domain=<lab trust domain> -e workload_user=<worker user>
```

```powershell
ansible-playbook -i '<cell host>,' examples/playbooks/spire/spire-federation-proof.yml `
  -e federated_trust_domain=<lab trust domain> -e workload_user=<worker user>
```

**Unfederate** removes the trust domain from both sides' workloads first, then the
relationships.

Neither SPIRE server has fetched the other's bundle, and no workload has run the proof, in
a live run yet.
