# Design: installing a Password Safe Resource Broker in a POV

Slice 5b of the POV feature — the half [#648](https://github.com/Weaverlab-xyz/vm-dashboard-community/pull/648)
deliberately left out. It fills `PovEnvironment.ps_application_host_id`.

Written before the code because the shape of this one is decided by a fact about
BeyondTrust rather than about this repo, and getting that fact wrong is what a design note
is for.

## The fact that decides everything

**A Resource Broker package comes from the tenant itself.** It is generated per Password
Safe tenant and downloaded from it — not a public image, not a versioned artifact this
dashboard could fetch, and not something a URL in config could point at. It carries an
**installer key** that pairs it with that tenant.

Two consequences follow immediately, and they rule out the shape slice 5 used:

* **The Gateway pattern does not transfer.** That slice names
  `beyondtrust/sra-jumpoint` in the agent's `policy.yaml` and the agent pulls it. There
  is no equivalent name here — the artifact is per-customer, so no policy file can name
  it in advance and no registry holds it.
* **The dashboard cannot obtain it.** Anything that starts "the dashboard downloads the
  package" is wrong before it is written.

So the customer has to put the package somewhere the dashboard can reach, and the
dashboard has to install it without ever having chosen it. That is a **staging** problem,
and this repo already has the machinery for it.

## The shape

```
customer downloads RB package from their Password Safe tenant
        │
        ▼
POST /api/config-mgmt/upload            ← already exists, with a secret scan
        │
        ▼
storage backend (S3 / Azure blob / GCS / local)
        │
        ▼
agent_ansible job on the POV's broker agent      ← already ships in agent 2.3
        │  asset      = the uploaded installer
        │  run_kind   = "vm", transport = "winrm"
        │  secret_vars = { <installer-key-var>: pov/<env>/rb_installer_key }
        │  extra_vars  = { zone: <resource zone> }
        │  login       = Skytap stored_credentials for that VM  ← §5
        ▼
ansible-winrm sibling on the LINUX broker VM
        │
        │  WinRM, across the POV's private network
        ▼
a WINDOWS VM in the POV → installer runs → RB registers with the tenant
```

**Two special VMs, not one.** The Resource Broker installer is a Windows program, so it
cannot live on the broker VM — that is a Linux Docker host by construction, and slice 5
keeps the Gateway there. A POV therefore has a Linux **broker** (agent + Gateway) and a
Windows **RB host**, and the broker reaches the second over WinRM.

That is not a complication; it removes one. An earlier draft of this note put the RB on
the broker VM itself, which meant an Ansible run in a sibling container having to reach
*its own host* — the ugliest part of the design, and the part most likely to be got subtly
wrong. Targeting a different machine is precisely the shape `agent_ansible` was built for:
"SSH/WinRM **to** a host".

Almost none of this is new. What follows is what already exists, and then the four things
that do not.

## What already exists

| | |
|---|---|
| `POST /api/config-mgmt/upload` | Takes a file into the storage backend. Runs an advisory secret scan that warns and never blocks |
| `ansible_local_service.asset_type` | Classifies `.ps1` as `powershell`, which the generator installs with `win_script` over WinRM. **`.exe` and `.msi` are not recognised** — see §1 |
| `ansible_local_service.generate_playbook_yaml` | Already generates a Windows play for a `.ps1`. The `.exe`/`.msi` wrapper does not exist yet |
| `chrweav/ansible-winrm` | The `run_kind="vm"` runner image, which handles both SSH and WinRM. Its name is not a coincidence — this is what it is for |
| `stored_credentials` | Declared in both `WRITE_CONTRACT` and the Skytap capability table since slice 1, and never implemented. §5 makes it real |
| `agent_ansible` | Ships in agent **2.3**, so unlike slice 5 this needs no agent rebuild |
| `agent_ansible_bundle` | Fetches the asset, wraps it, seals it to a per-fetch key, and hands it to the agent. The sibling has no bind mounts, so the bytes travel *in* the bundle |
| `RUN_META_KEYS.secret_vars` | Maps a **var name to a source ref**. The ref is what lands in the job row; the value is resolved at fetch time and never written down |
| `RUN_META_KEYS.epml_token_var` | The precedent for exactly this: "the NAME of the var an EPM-L installation token is bound to, never the token" |

That last row matters more than its size. EPM-L already solves "install a BeyondTrust
agent, keyed to a tenant, without the key touching the database" — and the RB installer key
is the same problem with a different product on the label. Slice 5b should look like it,
not like something new.

## 1. What the installer actually is, and the `.exe` asset-type gap

Grounded in [Resource Broker How to](https://beyondtrust.atlassian.net/wiki/spaces/BI/pages/1859027113/Resource+Broker+How+to)
(BeyondInsight space) rather than inferred. The artifact is
**`BeyondTrust.Agents.Bootstrapper.exe`**, and it takes MSI-style `NAME=value` arguments:

| | |
|---|---|
| `INSTALLKEY` | **Required for a silent install**, or the user is prompted. The per-tenant key |
| `ZONE` | **Also required for a silent install.** The resource zone this broker joins |
| `/quiet` | Makes it silent at all |
| `-l "install.log"` | The full install log |
| `INSTALLPATH` | Optional |
| `RESTART` | Optional — and see §2 before touching it |
| `USEPROXY` / `PROXYADDRESS` / `PROXYPORT` | Optional; out of scope for this slice |

So a silent install is:

```
BeyondTrust.Agents.Bootstrapper.exe /quiet -l "install.log" INSTALLKEY=<key> ZONE=<zone>
```

That settles the question this note previously left open: the key is an **argument**, not
a file the installer expects beside itself. No `win_copy` and no hand-written playbook.

### The gap

`_EXT_TYPE` knows `.yml`, `.sh`, `.ps1`, `.rpm` and `.deb`, and anything else **falls
through to `"playbook"`**. So a `.exe` uploaded today is handed to `ansible-playbook` as
though it were YAML and fails with "Unable to parse … did not contain a list of plays" —
which reads as a broken playbook rather than as a file that was never one.

Two ways out, and the first is better:

* **Add `.exe` (and `.msi`) as an asset type** with an `ansible.windows.win_package`
  wrapper. That collection is already in use — the `.ps1` branch calls `win_script` — and
  `win_package` has an `arguments:` field, which is exactly the shape above. One edit to
  `_EXT_TYPE`, one branch in `generate_playbook_yaml`, and every Windows installer in the
  product benefits rather than just this one.
* **Have the customer upload a `.ps1` wrapper.** Works today with no code change, and is
  the wrong trade: it needs *two* assets where the bundle carries one, and it makes every
  customer write the same script.

### `ZONE` is a second per-POV value, and it is not a secret

This is the parameter most likely to be missed, because "the installer needs a key" is the
half everyone remembers. A silent install without `ZONE` **prompts** — and a prompt in an
unattended run is not an error, it is a process that sits there until the run's timeout
kills it, with an install log that ends mid-dialog.

It is a zone name, not a credential, so it belongs in plain `extra_vars` alongside the
key's `secret_vars` entry. Storing it on the POV row (or in the environment's
`extra_data`) is enough; it does not need the encrypted per-resource treatment the key
gets.

**Refuse to queue a run with no zone**, in preflight, with the same discipline slice 5
applies to a missing deploy key. The alternative is a job that hangs for its full timeout
and reports nothing useful.

## 2. The installer key is a `secret_vars` entry, not a command-line parameter

The obvious reading of "pass the installer key as a command-line parameter" is
`ansible-playbook -e key=…`, and it should not be built that way: **argv is visible in
`ps`** for the life of the process, on a host inside a customer's environment.

Note this is about how the key reaches *Ansible*, not about how it reaches the installer.
Handing it to `win_package`'s `arguments:` is fine and is what the product expects; what
must not happen is the value appearing on the runner's own command line, where every
process on that host can read it.

### Do not set `RESTART=1`

Worth its own heading because the documentation's phrasing is easy to skim past: during a
**silent** install, `RESTART=1` restarts the machine *automatically*, with no prompt. That
would drop the WinRM session mid-play, and Ansible would report the failure of a step that
had in fact succeeded — the worst kind of wrong answer, because retrying reinstalls a
working broker.

If the RB genuinely needs a reboot, the play should use `ansible.windows.win_reboot`,
which waits for the host to come back and continues. Leave `RESTART` unset and let the
play own the reboot.

`secret_vars` gets the same result without that. It maps a var name to a source ref; the
job row holds the ref, `ansible_credentials.resolve` turns it into a value at the moment
the agent fetches the sealed bundle, and the runner receives it as a variable. It is
registered for redaction, so it cannot appear in Live Output either.

The key is stored the same way the Gateway deploy key is —
`pov/<env_id>/rb_installer_key`, Fernet-encrypted, resolvable from an external vault by
reference, carried by the config-migration tool.

## 3. Which VM is the RB host, and how it is chosen

`PovEnvironmentVM` already records `os_family` — `"linux"`, `"windows"` or blank — and
slice 3 was deliberate that blank means *unknown*, never a guess, "because a confident
wrong answer sends a Windows VM down the Password-Safe-over-SSH path". That column is what
makes the RB host selectable at all.

Selection should follow the broker VM's rule exactly: **an operator-chosen name, matched
exactly and case-insensitively, defaulting to something like `rb`.** Not "the first
Windows VM" — a POV template with a domain controller and a member server has two, and
picking one by position installs a Resource Broker on whichever the platform happened to
list first.

The run is then `run_kind="vm"`, `transport="winrm"`, `target_host` = that VM's
`private_ip`, `target_port` 5985 or 5986. `agent_ansible_meta.transport_for_guest_os`
already derives WinRM from a Windows guest, so the transport does not have to be typed.

**Refuse, loudly, if the named VM's `os_family` is not `windows`.** A Linux VM reached
over WinRM fails as a connection timeout, which reads as a firewall problem and sends an
SE to the wrong place entirely.

## 4. It widens what a POV broker may do, and that should be visible

Slice 3 generates the broker's `policy.yaml`, and slice 5 added `agent_gateway` to it.
This adds `agent_ansible`, which is a **materially larger grant** — the policy example
says so in as many words:

> `targets:` at the top of this file grants a port PROBE. This grants a playbook, running
> as root, on the hosts you name.

The `ansible:` block also needs its own `targets:` list, deliberately separate from the
discovery one. For a POV that list should be **the RB host's `/32` on 5985/5986 and
nothing else** — not the POV's whole VM list, and not the broker's own address. The RB
installs on one machine; granting playbook execution against every VM in the environment
because one of them needs an installer is the kind of widening nobody notices until it
matters.

It also needs `ansible.vm_image` (`chrweav/ansible-winrm`), because `run_kind="vm"`
selects it — and the operator has to have pulled it onto the broker VM, because the agent
will not pull an image for you.

And, as with slice 5: **a POV brokered before 5b has none of this**, so the same
re-broker remedy applies and the preflight should say so by name rather than letting the
job lease and be refused.

## 5. The credential comes from the platform: `stored_credentials`

**Decided.** A WinRM run needs a Windows login, and it comes from Skytap's own stored
credentials — `lab_platforms.CAPABILITIES["skytap"]["stored_credentials"]` is already
declared `True`, `stored_credentials` is already in `WRITE_CONTRACT` as
`(env_id, vm_id) -> [{text, notes}]`, and it has never been implemented. Slice 5b makes it
real.

It is the right source for three reasons. It **adds no new secret to this database** — the
value is read from the platform per run and never stored. It is **per-environment by
construction**, so two POVs from the same template do not share a credential row. And a
template's Windows VM already has a baked-in local administrator recorded there, because
that is what the field is for.

The two alternatives, and why not:

| | |
|---|---|
| **A per-POV credential the operator supplies** | Works, and is the fallback for a platform whose capability table says `stored_credentials: False`. As the primary path it is one more secret in this database and one more thing to paste per POV, for a value the platform already holds |
| **Password Safe itself** | `managed_account` already exists in `RUN_META_KEYS` and checks a credential out just-in-time, rotating it on check-in. Elegant, and **circular**: the RB is what gives Password Safe reach into this network, so it cannot be the source of the credential that installs it |

### The wrinkle: it is free text, not a username and a password

This is the part to get right, because the contract's shape gives it away —
`[{text, notes}]`, not `[{username, password}]`. Skytap stores what somebody typed into a
box. In practice that is `administrator / Password123`, or `administrator:Password123`, or
a sentence with the pair somewhere inside it.

So the adapter has to **parse**, and parsing is where this goes wrong quietly. Three rules:

* **Refuse rather than guess.** A `text` the parser cannot split into exactly one pair is
  an error naming the VM, not a best effort. A wrong username sends a WinRM auth failure
  back, which reads as a bad password and sends an SE to reset one.
* **Refuse on ambiguity too.** More than one entry, or one entry yielding more than one
  plausible pair, is a refusal — the fallback in the table above is what an operator
  reaches for when their template's credential box does not parse.
* **Never log the `text`.** It contains the password by definition. The parsed username is
  fine to name in a job log; nothing else from that field is.

The parsed pair maps onto the run as `login_user` plus a resolved password. Note the
password must reach `ansible_credentials` as a *value* at bundle-assembly time rather than
a ref — it is fetched from the platform, not stored — so this is the one credential in the
Config-Management path that does not begin life as a `secret_vars` source. Worth a look
during implementation: it may be cleanest to mint it into the per-POV config space just
long enough for the fetch, or to widen the resolver by one typed field.

WinRM also has to be *reachable and enabled* on that guest, which is a template-contract
question rather than a dashboard one — see §7.

## 6. What `ps_application_host_id` actually holds

Worth settling before the column is filled. The RB registering itself with the tenant is
Password Safe's business, not this dashboard's — the install hands over a key and the
product does the rest. So the dashboard should **read the id back** after the install
rather than assign one, most likely by listing brokers via `ps_api_service` and matching
on the name the install used.

If that read is not available, the honest thing is to leave the column NULL and record the
install in the job, rather than invent an id that means nothing. A column filled with a
guess is worse than an empty one — the next slice will join on it.

## 7. The template contract grows a second VM

Slice 3 documented what a POV template's broker VM must carry: Docker, an automatic
network, and the metadata runner. This adds a second machine to that contract, and it
should be written into `docs/integrations/skytap.md` beside the first rather than
discovered:

* a **Windows Server 2019 x64 or 2022 x64** VM, on the same automatic network — the
  versions [the Resource Broker docs](https://beyondtrust.atlassian.net/wiki/spaces/DOCS/pages/2477588518/Draft+Resource+Broker)
  name;
* **WinRM enabled and reachable** from the broker VM — `winrm quickconfig` territory. The
  policy example already defaults WinRM to 5985 over HTTP rather than 5986, and explains
  why: the credential reaches the run sealed to a per-fetch key rather than riding WinRM's
  own transport;
* a **known local administrator**, which is what §5's stored credentials read.

None of that is exotic for a POV template — it is what a Password Safe demo needs anyway.
It is worth stating because a missing piece of it fails as a WinRM timeout, and a WinRM
timeout looks like a network problem no matter which of the three is actually absent.

One sizing note. The bootstrapper is a **bundle**: it installs the VC++ 2010 and 2015-2019
redistributables, .NET Framework 4.7.2 and the .NET Core hosting bundle before it installs
the broker itself. That is minutes, not seconds, and often a fresh download on a clean
template. `ansible.max_runtime_minutes` defaults to 30 in the policy example, which is
enough — but the *job's* own expectations should match, and an operator watching Live
Output should be told what the silence is.

## What this is not

**Not a new agent verb.** Slice 5 needed one and paid an image-rebuild gate for it.
`agent_ansible` already ships, which means 5b can land without one — and that is worth
protecting. If the design starts drifting toward "a small handler that just runs the
installer", that is the moment to stop and reconsider, because it doubles the deployment
cost of the feature for convenience.

**Not an RB the dashboard versions or upgrades.** It installs what the customer staged.
Replacing it means uploading a newer package and running again; there is no upgrade path
to write and no version to track.

**Not a Linux Resource Broker.** The installer is a Windows program, so the Linux broker
VM is not a candidate however convenient it would be. Slice 5's Gateway stays on the Linux
broker; the two live on different machines on purpose.

## Slice boundary

In:

* `.exe` / `.msi` as an asset type, with a `win_package` wrapper play (§1)
* upload → storage (existing endpoint, possibly a POV-scoped listing)
* per-POV installer key, stored like the Gateway deploy key, delivered via `secret_vars`
* per-POV **resource zone**, plain (not a secret), refused at preflight when absent (§1)
* an operator-chosen **RB host VM name** on the POV, matched exactly, refused unless that
  VM's `os_family` is `windows` (§3)
* `stored_credentials` on the Skytap adapter, with a parser that refuses rather than
  guesses, plus the operator-supplied fallback for a platform that lacks the capability
  (§5)
* `agent_ansible` + an `ansible:` block in the generated broker policy, scoped to the RB
  host's `/32` on 5985/5986 (§4)
* a `POST /api/pov/managed/{id}/resource-broker` that preflights and queues the run
* reading `ps_application_host_id` back, or leaving it NULL and saying so (§6)
* the template contract's second VM, documented (§7)

Out:

* any attempt to obtain the package on the customer's behalf
* RB upgrades or versioning. The broker's own management service already checks the server
  for newer versions and auto-updates on cloud tenants — that is the product's job, and a
  dashboard that also tracked versions would be a second opinion nobody asked for
* proxy configuration (`USEPROXY` and friends). Real, documented, and a separate decision
* a Linux Resource Broker, or moving the Gateway off the Linux broker VM

## Sources

The product facts above are from BeyondTrust's own documentation rather than from reading
this repo, which is the whole reason this note exists:

* [Resource Broker How to](https://beyondtrust.atlassian.net/wiki/spaces/BI/pages/1859027113/Resource+Broker+How+to)
  — the bootstrapper's command-line parameters, the silent-install examples, the proxy
  options and the auto-update behaviour
* [Resource Broker](https://beyondtrust.atlassian.net/wiki/spaces/DOCS/pages/2477588518/Draft+Resource+Broker)
  — the supported Windows Server versions and the services a bundle installs

## Decisions taken

Recorded so a reader does not re-open them:

| | |
|---|---|
| The RB host is a **separate Windows VM**, not the broker | The installer is a Windows program; the Gateway stays on the Linux broker |
| The credential comes from **Skytap `stored_credentials`** | §5. No new secret in this database, per-environment by construction |
| The installer key rides **`secret_vars`**, the zone rides `extra_vars` | §1, §2. One is a credential, one is not |
| Delivery is **`agent_ansible`**, not a new agent verb | It already ships in agent 2.3, so no image-rebuild gate |

## Prerequisite

Slices 3, 4 and 5 have **never been run against live infrastructure** — no Skytap account,
no PRA appliance, no Password Safe tenant. 5b puts a fourth untested layer on top and its
first real test would involve a customer's tenant. Validating the stack below it first is
the cheaper order.
