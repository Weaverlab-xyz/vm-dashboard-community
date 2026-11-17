# Demo Cell architecture and workflow

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want the shape of a demo cell at once, or are explaining to somebody else what builds one, what wires it and what retires it.

Part of the [Demo profile](README.md). One page for the shape of the whole thing: what
builds a cell, what wires its resources, and what ends it. The [POV architecture](../../architecture.md)
page is the same exercise for a POV instance.

<figure style="margin:2rem 0">
<div style="overflow-x:auto">
<svg viewBox="0 0 860 560" role="img" aria-label="A demo cell's life in three phases: a presenter launches a cell from a blueprint and it passes launch checks before its resources are provisioned, each resource is wired through its own cloud's deploy path, then the cell runs, suspends and resumes, and is ended by the schedule sweeper with a 24 hour backstop on every resource." style="width:100%;min-width:720px;height:auto;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif">
<defs>
<marker id="arw" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,1 L9,5 L0,9 z" fill="currentColor"/></marker>
<marker id="arwd" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,1 L9,5 L0,9 z" fill="#94a3b8"/></marker>
<marker id="arwa" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,1 L9,5 L0,9 z" fill="#1d4ed8"/></marker>
</defs>
<g fill="currentColor" text-anchor="middle">
<rect x="24" y="24" width="812" height="130" rx="10" fill="#f8fafc" stroke="#e2e8f0"/>
<text x="40" y="46" text-anchor="start" font-size="11" font-weight="700" letter-spacing="1.2" fill="#64748b">1 · BUILD</text>
<rect x="44" y="70" width="150" height="56" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="119" y="92" font-size="11.5">Presenter</text>
<text x="119" y="108" font-size="10" fill="#475569">picks a blueprint, a name</text>
<text x="119" y="120" font-size="10" fill="#475569">and a lifetime</text>
<line x1="198" y1="98" x2="244" y2="98" stroke="currentColor" marker-end="url(#arw)"/>
<rect x="248" y="70" width="150" height="56" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="323" y="92" font-size="11.5">Launch checks</text>
<text x="323" y="108" font-size="10" fill="#475569">name · admission policy</text>
<text x="323" y="120" font-size="10" fill="#475569">change window · budget</text>
<line x1="402" y1="98" x2="448" y2="98" stroke="currentColor" marker-end="url(#arw)"/>
<rect x="452" y="70" width="150" height="56" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="527" y="92" font-size="11.5">Cell row</text>
<text x="527" y="108" font-size="10" fill="#475569">status: scheduled</text>
<text x="527" y="120" font-size="10" fill="#475569">built now, or when booked</text>
<line x1="606" y1="98" x2="652" y2="98" stroke="currentColor" marker-end="url(#arw)"/>
<rect x="656" y="70" width="156" height="56" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="734" y="92" font-size="11.5">Provision</text>
<text x="734" y="108" font-size="10" fill="#475569">one resource at a time,</text>
<text x="734" y="120" font-size="10" fill="#475569">as the launching user</text>
<rect x="24" y="170" width="812" height="168" rx="10" fill="#f8fafc" stroke="#e2e8f0"/>
<text x="40" y="192" text-anchor="start" font-size="11" font-weight="700" letter-spacing="1.2" fill="#64748b">2 · WIRE — EACH RESOURCE THROUGH ITS OWN CLOUD</text>
<rect x="44" y="208" width="120" height="44" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="104" y="227" font-size="11">VMs</text>
<text x="104" y="242" font-size="10" fill="#475569">1 to 10 per item</text>
<rect x="176" y="208" width="120" height="44" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="236" y="227" font-size="11">K8s cluster</text>
<text x="236" y="242" font-size="10" fill="#475569">GKE · EKS · AKS</text>
<rect x="308" y="208" width="120" height="44" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="368" y="227" font-size="11">Database</text>
<text x="368" y="242" font-size="10" fill="#475569">cloud-managed</text>
<text x="640" y="228" font-size="10.5" fill="#475569">AWS · Azure · GCP, chosen per resource,</text>
<text x="640" y="244" font-size="10.5" fill="#475569">so one cell can span clouds</text>
<line x1="104" y1="254" x2="104" y2="274" stroke="currentColor" marker-end="url(#arw)"/>
<line x1="236" y1="254" x2="236" y2="274" stroke="currentColor" marker-end="url(#arw)"/>
<line x1="368" y1="254" x2="368" y2="274" stroke="currentColor" marker-end="url(#arw)"/>
<rect x="44" y="278" width="384" height="40" rx="7" fill="#eff6ff" stroke="#1d4ed8"/>
<text x="236" y="296" font-size="11" fill="#1d4ed8" font-weight="600">The cloud's normal deploy path</text>
<text x="236" y="310" font-size="10" fill="#1d4ed8">PRA jump items · Password Safe · Entitle · inventory</text>
<rect x="470" y="278" width="170" height="40" rx="7" fill="#ffffff" stroke="#94a3b8" stroke-dasharray="4 3"/>
<text x="555" y="296" font-size="10">OT cell adds a Web Jump,</text>
<text x="555" y="310" font-size="10">a tunnel and a DMZ broker</text>
<rect x="652" y="278" width="160" height="40" rx="7" fill="#ffffff" stroke="#94a3b8" stroke-dasharray="4 3"/>
<text x="732" y="296" font-size="10">Agent cell: SPIFFE identity,</text>
<text x="732" y="310" font-size="10">no static secret</text>
<rect x="24" y="354" width="812" height="186" rx="10" fill="#f8fafc" stroke="#e2e8f0"/>
<text x="40" y="376" text-anchor="start" font-size="11" font-weight="700" letter-spacing="1.2" fill="#64748b">3 · RUN AND RETIRE</text>
<rect x="44" y="396" width="130" height="54" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="109" y="418" font-size="11">active</text>
<text x="109" y="433" font-size="10" fill="#475569">every resource up</text>
<line x1="178" y1="423" x2="212" y2="423" stroke="currentColor" marker-end="url(#arw)"/>
<rect x="216" y="396" width="150" height="54" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="291" y="418" font-size="11">suspend · resume</text>
<text x="291" y="433" font-size="10" fill="#475569">button or schedule</text>
<line x1="370" y1="423" x2="404" y2="423" stroke="currentColor" marker-end="url(#arw)"/>
<rect x="408" y="396" width="150" height="54" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="483" y="418" font-size="11">end time</text>
<text x="483" y="433" font-size="10" fill="#475569">movable with Change end</text>
<line x1="562" y1="423" x2="596" y2="423" stroke="#1d4ed8" marker-end="url(#arwa)"/>
<rect x="600" y="396" width="212" height="54" rx="7" fill="#eff6ff" stroke="#1d4ed8"/>
<text x="706" y="418" font-size="11" fill="#1d4ed8" font-weight="600">Sweeper destroys the cell</text>
<text x="706" y="433" font-size="10" fill="#1d4ed8">each resource, its own destroy</text>
<line x1="706" y1="452" x2="706" y2="472" stroke="#94a3b8" stroke-dasharray="4 3" marker-end="url(#arwd)"/>
<rect x="540" y="476" width="272" height="40" rx="7" fill="#ffffff" stroke="#94a3b8" stroke-dasharray="4 3"/>
<text x="676" y="494" font-size="10">Backstop: every resource also expires</text>
<text x="676" y="508" font-size="10">24 hours after the cell's end</text>
<text x="44" y="486" text-anchor="start" font-size="10.5" fill="#475569">Throughout: cost is tracked per cell against the monthly budget,</text>
<text x="44" y="503" text-anchor="start" font-size="10.5" fill="#475569">and one failed resource never stops the rest from being built.</text>
</g>
</svg>
</div>
<figcaption style="font-size:.85rem;color:#475569;margin-top:.5rem">A demo cell's life in three phases. The dashed boxes are cell-specific additions; everything else is the same deploy path a plain VM, cluster or database uses.</figcaption>
</figure>

## Build

A demo cell is a group of resources with **one lifetime, one suspend schedule and one
Destroy button**. There are four shapes:

| Shape | What it is | Page |
|---|---|---|
| **Custom cell** | Any mix of VMs, Kubernetes clusters and databases, on any of AWS, Azure and GCP, from a blueprint a **Cell Builder** wrote. | [Custom Demo Cell](custom-demo-cell.md) |
| **OT cell** | A simulated plant: PLC simulators and a web HMI in an egress-less subnet, reached only through a Gateway. | [OT Demo Cell](ot-demo-cell.md) |
| **Network cell** | **Preview.** A VyOS router with no inbound rule, reached through a recorded Shell Jump. | [Network Demo Cell](net-demo-cell.md) |
| **Agent cell** | **Preview.** A non-human principal attested by SPIRE. | [Agent Demo Cell](agent-demo-cell.md) |

The OT, network and agent cells have a fixed shape and cannot be members of a custom cell.
The rest of this page follows the custom cell, because it is the one that is composed
rather than fixed.

Who may do what is a role question: a **Demo Presenter** launches, books, suspends and
destroys cells, and a **Cell Builder** also writes the blueprints. See the
[Demo hub](demo-hub.md).

Launching refuses what the dashboard can detect without touching a cloud, at booking time
rather than on the morning of the demo: a bad name (1 to 24 lowercase letters, digits and
hyphens), a field a cloud has since tightened, Kubernetes management being off, an
admission-policy refusal judged at the cell's start
time, and a booking that would exceed the monthly budget. The cell is then recorded as
**scheduled**. Launching *Now* builds it at once; booking it for later leaves it to the
sweeper.

Provisioning creates the resources one at a time, **as the person who launched the cell**,
so their workstream, the admission policy, secret bindings and change-window approval all
apply. One resource failing does not stop the others: the cell goes **failed** with its
other resources kept, so Destroy still cleans up.

## Wire

There is no cell-specific wiring for a custom cell. Every resource goes through **its own
cloud's normal deploy path**, which is what puts it into PRA, Password Safe and Entitle and
into inventory. That is why a cell resource is also an ordinary row on its own page: the
cloud console, Kubernetes or Databases.

The fixed cells add the parts that make their story work: the OT cell a Web Jump and a
protocol tunnel per endpoint and an optional DMZ broker, the agent cell a SPIFFE identity
instead of a stored secret.

Unlike a POV, an estate cell has **no customer share link**. It is shown by the person
presenting it, through PRA jump items and tunnels.

## Run and retire

| Status | Meaning |
|---|---|
| **scheduled** | Booked; nothing exists yet. |
| **provisioning** | Being built. |
| **active** | Every resource is up. |
| **suspended** | Stopped by **Suspend** or the schedule. |
| **failed** | A resource failed. The others are kept. |
| **destroying** | Being torn down. |
| **destroyed** | Gone. |

**Suspend and Resume** stop or start the whole cell: VMs stop, clusters scale their node
pools to zero and databases stop. A blueprint's **suspend schedule** does the same at its
times, and while a cell is suspended a database that its cloud restarts on its own is
stopped again. **Change end** moves the end time.

A sweeper runs every few minutes. It starts booked cells that are due, **ends cells whose
end time has passed** (as the person who launched them), and refreshes the rest. Ending a
cell destroys each resource through the same path its own Destroy button uses; a resource
still building is torn down on a later pass. This does not depend on the auto-delete
feature being on.

As a backstop, every resource also carries its **own** auto-delete timer set to the cell's
end plus 24 hours, so the ordinary reaper still collects it if the cell record were ever
lost.

Throughout, [cost](demo-hub.md) is tracked per cell and counted against the monthly budget,
which a presenter can be given their own of.

A POV instance carries these cells over with a per-VM role rather than a cell record. See
[Demo cells in a POV](../pov/demo-cells.md).
