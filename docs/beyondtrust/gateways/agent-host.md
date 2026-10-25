# A Gateway on an agent host

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want a PRA Gateway running on a remote agent's host rather than in a cloud.

Part of [BeyondTrust Gateway Hosts](../gateways.md).

A [remote agent](../../platform/spiffe/remote-agents/enrolment.md#the-beyondtrust-gateway) can run a Gateway **on its
own host**, inside a network the dashboard cannot route to at all. The dashboard never
touches a cloud API for this one — it queues an `agent_gateway` job and the agent starts a
privileged container beside itself. Today the driver is the
[POV feature](../../profiles/pov/gateway-and-broker.md#the-pov-gateway), which generates the whole policy for a
control node VM it created.

**It is not in this tab, and that is not an oversight.** The `gateways` inventory is
per-cloud — a row's `cloud` is one of `aws`, `azure`, `gcp` — and nothing writes a row for a
Gateway on somebody's own hardware. So an agent-installed Gateway is absent from:

- the **Gateways tab** and the **Gateways tile** count;
- the **reconcile pass**, which compares rows against what is actually running in a cloud;
- the **reference-counted lifecycle** — nothing holds a reference on it, and the idle
  teardown cannot reach it.

You see it on its **POV row** instead, which shows whether a Gateway is named and whether a
deploy key is stored, plus a button for the live PRA answer.

## It is its own PRA Gateway, not another node

This is the important difference from
[one Gateway, many nodes](../gateways.md#one-pra-gateway-many-nodes). Cloud hosts all launch with the
*same* per-cloud deploy key, which is why they cluster into a single PRA Gateway. An agent-installed Gateway
takes a deploy key **you paste onto that POV**, so it is a separate, separately-named
Gateway — nothing joins the cloud one, and adding one gives the cloud Gateway no capacity.

It may also live in an entirely **different PRA appliance**. The tenant comes from the
POV's explicit entry in the BeyondTrust tenant registry, and a POV whose tenant was deleted
or disabled is an **error** rather than a quiet fall back to the default — because the
fallback would install a customer's Gateway into somebody else's appliance.

Capability requirements are the same as the GCP host in the table below: a tunnel needs
`NET_ADMIN`, `NET_RAW`, `IPC_LOCK` and `/dev/net/tun`. Docker cannot grant that set
granularly, which is why the agent's `policy.yaml` has to say `privileged: true` and why the
agent refuses to start one without it. Everything else about the agent side — the four
grants, the 2.4.0 image floor — is in
[Remote Agents](../../platform/spiffe/remote-agents/enrolment.md#the-beyondtrust-gateway).

## Its status is read live, never stored

The POV row's Gateway state is a question asked of PRA at the moment you press the button,
not a column. A stored status is how a row still reads *connected* three weeks after the
environment was suspended.

`connected` is deliberately **tri-state**: some appliance versions spell the field
differently and simply do not answer, and reporting that as *disconnected* would send you to
debug a Gateway that is fine. So "unknown — check the appliance directly" is a distinct
result from "disconnected".
