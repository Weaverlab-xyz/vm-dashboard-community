# The POV control node on a cloud

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you need to know what the control node VM does in a cloud POV, and how to reach it.

Part of [Running POVs on a public cloud](../public-cloud.md).

A POV's control node is the VM that carries the dashboard agent, the Gateway and the Password
Safe Resource Broker. On Skytap the template ships it, the dashboard writes a payload into
its `user_data`, and a runner inside the guest fetches and executes it. A cloud does the
last part for you — cloud-init runs user-data — but only **on first boot**. Handing a
payload to an instance that is already up does nothing at all, silently.

So on a cloud the control node VM is **created by the control node install, not by the build**, with its
bootstrap already in user-data. That ordering is forced by the agent's policy, which
grants the POV's target addresses one at a time — and those addresses do not exist until
the targets do. The sequence is: build the targets → wait for them → read their addresses
→ mint the enrolment code → create the control node VM carrying both.

Two consequences worth knowing:

- **An environment shows one fewer VM than its template until the control node lands.** That is
  the honest reading of "the control node is not installed yet", rather than a VM sitting there
  doing nothing.
- **Pressing Rebuild control node rebuilds the VM.** The old one is terminated first, which
  destroys its agent-state volume with it. On Skytap a rebuild has to remember to delete
  that volume — an agent that already enrolled never redeems a second code, so a surviving
  volume gives a container that starts fine and 401s forever. Here it is clean by
  construction.

The control node VM's image must have **cloud-init and Docker**. Build it with the Packer
tooling and reference it from the template.

**A cloud control node is never auto-detected, and must not be.** On Skytap the control node is found
by its OS -- the only Linux VM in the template -- because the template was authored
elsewhere and the dashboard is adopting one of its guests. A cloud inverts that: the
template designates its control node by **role**, and the dashboard *builds* it. So after the
create, the instance is identified by the **id the driver just returned**, with the role's
name as a fallback and no inference at any point. Inferring here would pick "the only
Linux VM" out of rows that by then include this POV's Linux *workload* guests, record one
of those as the control node, and look completely successful -- the bootstrap did reach the real
control node via cloud-init, and the agent does enrol. What breaks later is everything that
excludes the control node from a target list, which would then exclude the wrong machine.

**The spent enrolment code stays in user-data.** A cloud's user-data can only be rewritten
while the instance is stopped, so unlike the Skytap path there is nothing to clear once
the code has been redeemed. The exposure is small — the code is single-use, fifteen
minutes old by then, and IMDSv2 is required so reading it needs a token obtained from on
the guest — but it is a real difference rather than an oversight.
