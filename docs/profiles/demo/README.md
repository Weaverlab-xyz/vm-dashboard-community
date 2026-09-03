# Demo profile

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you are showing the dashboard to someone and want the story rather than the feature list.

The default profile, and what every existing install already is. A demo instance manages
**your** estate: your cloud accounts, your hypervisors, your images. You show it to people.

That is the difference from the [POV profile](../pov/README.md), where each environment
belongs to a customer and points at the customer's own BeyondTrust tenant. See
[the gate](../README.md#the-gate) for why the two cannot be the same instance.

## Pages

| Page | What's in it |
|---|---|
| [Personas](personas/README.md) | One page per role — what that person owns, the four-layer story in their language, and the use cases to run for them. |
| [OT Demo Cell](ot-demo-cell.md) | A simulated OT/ICS plant cell — PLC simulators and a web SCADA/HMI in an egress-less subnet, reached only through a Gateway. The air gap *is* the demo. |
| [OT protocol clients on Windows](ot-protocol-clients.md) | Setting up a rep machine to read that cell: the four Python clients, a runnable snippet per protocol, and why DNP3 answers nothing. |

## Everything else is a capability doc

Almost every feature a demo instance has is documented once at the
[docs root](../../README.md) rather than here, because the page describing how to deploy an
EC2 instance is the same page whether you are demoing or running a lab. Each of those pages
carries `**Profile:** demo` in its header block.

What lives *here* is the material that only makes sense as a demo: the per-role narratives,
and the purpose-built demo environments.
