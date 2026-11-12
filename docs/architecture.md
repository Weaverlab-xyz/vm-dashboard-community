# POV architecture and workflow

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you want the shape of the whole thing at once, or are explaining to somebody else what a POV is made of.

Part of [A POV Instance](profiles/pov/README.md). One page for the shape of the whole thing:
what builds a POV, what wires it, and what retires it.

<figure style="margin:2rem 0">
<div style="overflow-x:auto">
<svg viewBox="0 0 860 660" role="img" aria-label="A POV's life in three phases: it is built on a lab platform against its own BeyondTrust tenants, set up in an ordered chain of steps with a row of optional ones hanging off it, then run for the call and reaped afterwards." style="width:100%;min-width:720px;height:auto;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif">
<defs>
<marker id="arw" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,1 L9,5 L0,9 z" fill="currentColor"/></marker>
<marker id="arwd" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,1 L9,5 L0,9 z" fill="#94a3b8"/></marker>
<marker id="arwa" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,1 L9,5 L0,9 z" fill="#1d4ed8"/></marker>
</defs>
<g fill="currentColor" text-anchor="middle">
<rect x="24" y="44" width="812" height="140" rx="10" fill="#f8fafc" stroke="#e2e8f0"/>
<text x="40" y="66" text-anchor="start" font-size="11" font-weight="700" letter-spacing="1.2" fill="#64748b">1 · BUILD</text>
<rect x="44" y="82" width="130" height="48" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="109" y="111" font-size="12">POV Presenter</text>
<text x="249" y="88" font-size="10.5" fill="#475569">picks a blueprint</text>
<text x="249" y="100" font-size="10.5" fill="#475569">and this POV&#39;s tenants</text>
<line x1="178" y1="112" x2="316" y2="112" stroke="currentColor" marker-end="url(#arw)"/>
<rect x="320" y="82" width="110" height="48" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="375" y="111" font-size="12">Build</text>
<text x="493" y="76" font-size="10" fill="#475569">provisions on</text>
<line x1="434" y1="98" x2="550" y2="86" stroke="currentColor" marker-end="url(#arw)"/>
<text x="493" y="152" font-size="10" fill="#475569">binds tenants</text>
<line x1="434" y1="116" x2="550" y2="138" stroke="currentColor" marker-end="url(#arw)"/>
<rect x="556" y="62" width="260" height="44" rx="7" fill="#f1f5f9" stroke="#94a3b8"/>
<text x="686" y="80" font-size="11.5" font-weight="600">Lab platform</text>
<text x="686" y="96" font-size="10" fill="#475569">Skytap · AWS · Azure</text>
<rect x="556" y="122" width="260" height="44" rx="7" fill="#f1f5f9" stroke="#94a3b8"/>
<text x="686" y="140" font-size="11.5" font-weight="600">This POV&#39;s tenants</text>
<text x="686" y="156" font-size="10" fill="#475569">PRA · Password Safe · Entitle</text>
<rect x="24" y="200" width="812" height="262" rx="10" fill="#f8fafc" stroke="#e2e8f0"/>
<text x="40" y="222" text-anchor="start" font-size="11" font-weight="700" letter-spacing="1.2" fill="#64748b">2 · SET UP — IN ORDER</text>
<rect x="44" y="244" width="89" height="50" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="88" y="264" font-size="10">Use cases</text>
<text x="88" y="278" font-size="10">chosen</text>
<line x1="137" y1="269" x2="153" y2="269" stroke="currentColor" marker-end="url(#arw)"/>
<rect x="157" y="244" width="89" height="50" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="201" y="264" font-size="10">Environment</text>
<text x="201" y="278" font-size="10">running</text>
<line x1="250" y1="269" x2="266" y2="269" stroke="currentColor" marker-end="url(#arw)"/>
<rect x="270" y="244" width="89" height="50" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="314" y="264" font-size="10">Guest OS</text>
<text x="314" y="278" font-size="10">known</text>
<line x1="363" y1="269" x2="379" y2="269" stroke="currentColor" marker-end="url(#arw)"/>
<rect x="383" y="244" width="89" height="50" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="427" y="264" font-size="10">Control node</text>
<text x="427" y="278" font-size="10">agent enrolled</text>
<line x1="476" y1="269" x2="492" y2="269" stroke="currentColor" marker-end="url(#arw)"/>
<rect x="496" y="244" width="89" height="50" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="540" y="264" font-size="10">PRA</text>
<text x="540" y="278" font-size="10">Gateway</text>
<line x1="589" y1="269" x2="605" y2="269" stroke="currentColor" marker-end="url(#arw)"/>
<rect x="609" y="244" width="89" height="50" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="653" y="264" font-size="10">VMs</text>
<text x="653" y="278" font-size="10">wired</text>
<line x1="702" y1="269" x2="718" y2="269" stroke="#1d4ed8" marker-end="url(#arwa)"/>
<rect x="722" y="244" width="89" height="50" rx="7" fill="#eff6ff" stroke="#1d4ed8"/>
<text x="766" y="264" font-size="10" fill="#1d4ed8" font-weight="600">Customer</text>
<text x="766" y="278" font-size="10" fill="#1d4ed8" font-weight="600">share link</text>
<line x1="427" y1="296" x2="427" y2="324" stroke="#94a3b8" stroke-dasharray="4 3"/>
<line x1="114" y1="324" x2="738" y2="324" stroke="#94a3b8" stroke-dasharray="4 3"/>
<line x1="114" y1="324" x2="114" y2="346" stroke="#94a3b8" stroke-dasharray="4 3" marker-end="url(#arwd)"/>
<line x1="270" y1="324" x2="270" y2="346" stroke="#94a3b8" stroke-dasharray="4 3" marker-end="url(#arwd)"/>
<line x1="426" y1="324" x2="426" y2="346" stroke="#94a3b8" stroke-dasharray="4 3" marker-end="url(#arwd)"/>
<line x1="582" y1="324" x2="582" y2="346" stroke="#94a3b8" stroke-dasharray="4 3" marker-end="url(#arwd)"/>
<line x1="738" y1="324" x2="738" y2="346" stroke="#94a3b8" stroke-dasharray="4 3" marker-end="url(#arwd)"/>
<rect x="44" y="352" width="140" height="50" rx="7" fill="#ffffff" stroke="#94a3b8" stroke-dasharray="4 3"/>
<text x="114" y="372" font-size="10">Resource Broker</text>
<text x="114" y="386" font-size="10">installer</text>
<rect x="200" y="352" width="140" height="50" rx="7" fill="#ffffff" stroke="#94a3b8" stroke-dasharray="4 3"/>
<text x="270" y="379" font-size="10">Resource Broker</text>
<rect x="356" y="352" width="140" height="50" rx="7" fill="#ffffff" stroke="#94a3b8" stroke-dasharray="4 3"/>
<text x="426" y="379" font-size="10">Entitle agent</text>
<rect x="512" y="352" width="140" height="50" rx="7" fill="#ffffff" stroke="#94a3b8" stroke-dasharray="4 3"/>
<text x="582" y="372" font-size="10">Password Safe</text>
<text x="582" y="386" font-size="10">credentials in PRA</text>
<rect x="668" y="352" width="140" height="50" rx="7" fill="#ffffff" stroke="#94a3b8" stroke-dasharray="4 3"/>
<text x="738" y="372" font-size="10">OT HMI access</text>
<text x="738" y="386" font-size="10">through Entitle</text>
<text x="426" y="424" font-size="10.5" fill="#475569">Optional — none of these holds up the share link, and a POV that does not test the product settles them grey.</text>
<text x="426" y="441" font-size="10.5" fill="#475569">The broker VM is created last: its policy needs addresses the targets do not have until they exist.</text>
<rect x="24" y="478" width="812" height="170" rx="10" fill="#f8fafc" stroke="#e2e8f0"/>
<text x="40" y="500" text-anchor="start" font-size="11" font-weight="700" letter-spacing="1.2" fill="#64748b">3 · RUN AND RETIRE</text>
<rect x="60" y="518" width="136" height="54" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="128" y="540" font-size="11">warm-up booking</text>
<text x="128" y="555" font-size="10" fill="#475569">20 min lead, fires once</text>
<text x="221" y="539" font-size="10" fill="#475569">then</text>
<line x1="200" y1="545" x2="242" y2="545" stroke="currentColor" marker-end="url(#arw)"/>
<rect x="246" y="518" width="136" height="54" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="314" y="540" font-size="11">Ready for demo</text>
<text x="314" y="555" font-size="10" fill="#475569">worst row wins</text>
<text x="407" y="539" font-size="10" fill="#475569">joins</text>
<line x1="386" y1="545" x2="428" y2="545" stroke="#1d4ed8" marker-end="url(#arwa)"/>
<rect x="432" y="518" width="170" height="54" rx="7" fill="#eff6ff" stroke="#1d4ed8"/>
<text x="517" y="540" font-size="11" fill="#1d4ed8" font-weight="600">Customer access</text>
<text x="517" y="555" font-size="10" fill="#1d4ed8">share link · accessor page</text>
<text x="627" y="539" font-size="10" fill="#475569">finally</text>
<line x1="606" y1="545" x2="648" y2="545" stroke="currentColor" marker-end="url(#arw)"/>
<rect x="652" y="518" width="152" height="54" rx="7" fill="#ffffff" stroke="#cbd5e1"/>
<text x="728" y="540" font-size="11">idle-suspend</text>
<text x="728" y="555" font-size="10" fill="#475569">then auto-delete</text>
<text x="430" y="602" font-size="10.5" fill="#475569">Throughout: a reconcile sweep every 10 minutes reads each platform and writes the runstate back.</text>
<text x="430" y="619" font-size="10.5" fill="#475569">Every row reports how old it is, because a remembered value presented as a current one is the whole bug.</text>
</g>
</svg>
</div>
<figcaption style="font-size:.85rem;color:#475569;margin-top:.5rem">A POV&#39;s life in three phases. The lab platform and this POV&#39;s own BeyondTrust tenants are the two things it is built against; steps 5, 6 and 8 are optional and run beside the chain rather than in it.</figcaption>
</figure>

## Build

A POV instance is a **second** dashboard beside the one running your own estate — same image,
same code, its own database, its own users. It exists for one reason, **tenancy**: an estate
instance resolves its BeyondTrust tenant from the global singletons, while a POV instance holds
a *registry* of many named tenants, because several POVs run at once and each has its own PRA
appliance and Password Safe Cloud tenant. That is the right-hand box in the first band, and
[A POV Instance](profiles/pov/README.md) is the long version of why it has to be that way.

The left-hand box is a **lab platform** — the thing a POV environment actually runs on. Skytap
was the first; a POV may also run on one or more public clouds. Which platforms are selectable
comes from the `services/lab_platforms` registry, so a platform with no driver cannot be picked.
See [Skytap](profiles/pov/skytap.md) and [public cloud](profiles/pov/public-cloud.md).

Who may do the building is a role question, not a flag question: a **POV Presenter** sees only
their own POVs, a **POV Manager** sees and manages every one, and a **POV Template Builder**
builds the blueprints and creates no POV itself. See [Roles](admin/permissions/roles.md).

## Set up

**If this is your first POV, read [Your first POV, end to end](profiles/pov/first-pov.md)
instead of this page** — it walks the steps in order, says what each one is for, and is the one
place that lists them.

Every POV walks the same steps, and the POV's **Setup** tab lists them in this order with
one button each. A POV that is not finished opens there by default. A step that depends on an
unfinished one is greyed out and names the step to finish first.

Only real dependencies lock a step. **Resource Broker, Entitle agent and Password Safe
credentials in PRA are optional and never hold up the customer share link**, which is why the
diagram hangs them off the chain rather than putting them in it.

Two of them are worth reading before you need them:

- **PRA Gateway and Resource Broker** — [gateway and broker](profiles/pov/gateway-and-broker.md),
  and [the design note](profiles/pov/design/resource-broker.md) for why the broker VM is built
  last rather than with the rest of the environment.
- **VMs wired** — [wiring](profiles/pov/wiring.md). This is the step that puts the POV's guests
  into PRA and Password Safe, so it is the one whose failures are quietest.

What the customer then gets — the share link, the accessor page, and the use-case checklist they
tick as you go — is [customer access](profiles/pov/customer-access.md).

## Run and retire

A POV's `runstate` is a **remembered** value, and two things change it that this dashboard does
not: the platform's own idle timer, and anybody with a login to the platform. So a reconcile
sweep runs every ten minutes, writes back what the platform says, and every row reports how old
its answer is. Opening the **Overview** tab makes no calls at all; **Check now** is the button
that refreshes those inputs.

Before a call, book a **warm-up** rather than pressing Start when the customer joins — a
suspended lab can take several minutes to resume. The booking fires at most once and is only as
precise as the reconcile pass, so a 20-minute lead really means 10 to 20 minutes. It also books
a ready check for five minutes before the demo, so the badge you open the POV to is current.

Afterwards the lab suspends itself on the platform's idle timer, or on a schedule the dashboard
supplies where the platform has none, and the auto-delete timer reaps the POV. All of this is
[keeping a POV true, and reaping it](profiles/pov/lifecycle.md); on a public cloud the suspend
half is [idle suspend](profiles/pov/public-cloud/idle-suspend.md), and it matters more there
than on Skytap, because stopping a cloud VM halts compute only — disks, public IPs and the
network bill run for the whole evaluation.
