# Network Demo Cell

> **Audience:** presenter · **Profile:** `demo` · **Read this when:** you are showing an emergency firewall change that nobody had to be handed a password for.

> **Preview.** The bake has never been run against a live VyOS image: that a Packer
> shell provisioner configures one at all, and that the baked account is reachable once
> deployed, are both unobserved. Off by default; turn it on with the **Network Demo
> Cell** preview toggle in Settings. Work through the
> [E2E verification checklist](#e2e-verification-checklist) on your own image before you
> put this in front of anyone.

The dashboard can stand up a **VyOS router/firewall** — a real network OS, with a real
`configure` mode — in a cloud sandbox's private subnet, with **no external IP and no
inbound rule**, and layer the BeyondTrust stack on top. Same **provisioning + three
layers** model as [Cloud VMs](../../cloud-vms.md); the network twist is that the device
being demoed *is* a firewall, so "who may reconfigure it" and "what it permits" are
visibly different questions:

- **Provisioning** *(stand it up)* — deploy a VM from the Packer-baked **`vyos-cell`**
  image ([`provisioners/net/vyos-cell.sh`](https://github.com/Weaverlab-xyz/vm-dashboard-community/blob/main/provisioners/net/README.md)).
  The image carries an administrator account, SSH, and a baseline firewall ruleset that
  is **attached and empty** — so the demo's first `drop` rule takes effect on `commit`
  rather than needing a second, off-story step to wire it in.
- **Layer 1 — PRA** *(reach it)* — a **Shell Jump** over SSH, recorded, inherited from
  the cloud's normal VM deploy path. **That is the whole access layer.** There is no Web
  Jump and no protocol tunnel: a firewall needs a shell and nothing else, which is why
  this cell is a fraction of the size of the [OT cell](ot-demo-cell.md).
- **Layer 2 — Password Safe** *(manage its secrets)* — *optional, default on.* The
  image's administrator account is onboarded as a managed system + account. Read
  [What Password Safe does here](#what-password-safe-does-here) before you promise
  rotation — it is narrower than on a Linux VM, for a reason worth understanding.
- **Layer 3 — Entitle** *(grant time-boxed access)* — **not part of this cell**, and
  that is a decision rather than a gap. Entitle's SSH integration creates ephemeral
  accounts with `useradd`, which is not how VyOS manages users, and VyOS ships no
  official management site for a Web Jump to render or Entitle to grant. The
  expiring-window beat is told with **Password Safe's checkout window** instead: the
  credential lapses and is rotated shut, so the copy someone wrote down stops working.

**GCP only, for now.** The [OT cell](ot-demo-cell.md) reached three clouds in later
phases once its shape had settled, and this feature has a sharper reason to do the same:
what a VyOS guest does with each cloud's key injection is the least certain thing about
it, and answering that once is cheaper than answering it three times wrong. The whole
feature is gated on **`pra_enabled`** — a cell without PRA is a device nobody can reach.

---

## There is no cell job, and that is the design

The OT cell has an `ot_cell_deploy` parent job because it provisions a Web Jump and one
protocol tunnel per brokered endpoint once the VM is up; the parent exists to do that
wiring and own its teardown.

A network cell wires nothing. The plain `gce_deploy` path already provisions the Shell
Jump, the Password Safe onboarding, the shared-Gateway reference, the expiry stamp and
the inventory row — and a firewall needs no more than that. So **a cell is an ordinary
GCE deploy row** carrying a marker, a forced onboarding method and its own preflight
refusals.

The consequence is the useful part: **Destroy and the auto-delete timer already work.**
The row they act on is a normal deploy row, so the VM, the Shell Jump and the Password
Safe registration are reaped by the paths that reap every other VM, with nothing
cell-specific to keep correct.

## Before you deploy

- [ ] The **Network Demo Cell preview** switched on in Settings.
- [ ] A **VyOS image in your project.** You supply it — VyOS rolling builds are free,
      LTS and the marketplace listings are not, and that is not the dashboard's call to
      make. Import it, then bake `vyos-cell.sh` onto it from the **Build Image** tab.
- [ ] **A Gateway** on the subnet the cell will land in. The deploy refuses without one
      rather than building a device it cannot reach.
- [ ] **Which VyOS release train** the image is. You assert this on the form; the
      dashboard cannot read it off an image. See below.
- [ ] A Password Safe functional account on an **SSH platform**, if you are onboarding.

## The release train is your assertion

VyOS reorganised the firewall tree in 1.4. The commands you are about to type in front
of an audience differ by train:

| Train | Baseline ruleset | The demo's rule |
|---|---|---|
| **1.4 and later** | `set firewall ipv4 name BLOCKLIST …` | `set firewall ipv4 name BLOCKLIST rule 10 action drop` |
| **1.3** | `set firewall name BLOCKLIST …` | `set firewall name BLOCKLIST rule 10 action drop` |

The form asks which one, the same way the OT cell's form asks what runtime its image was
baked with, and for the same reason: nothing in a cloud image's metadata says what a
provisioner put inside it, and **getting it wrong looks like a typo rather than a
version mismatch** — which is the worst way for a demo to fail, because you will try to
fix the spelling.

## What Password Safe does here

The cell is always onboarded with the **traditional SSH method**, never the cloud-native
plugin, and the deploy **refuses** when the configured functional account is bound to
one.

That refusal is the important part. All three cloud-native plugins manage a guest
*through an agent* — Systems Manager on AWS, waagent on Azure, the Google guest agent on
GCP — and **VyOS runs none of them**. Onboarded on the GCP default, the cell would
onboard *successfully*, attach to a platform that can never talk to it, and look healthy
until the first rotation — which is long after the demo.

> **Rotation needs a VyOS platform, and it is yours to build.** The credential is
> vaulted, injected and never seen the moment the cell is onboarded — that is the whole
> of the "nobody is handed the firewall password" story, and it works today. *Rotating*
> it needs a Password Safe platform whose change command runs
> `configure; set system login user … authentication plaintext-password …; commit; save`
> in vbash, because VyOS regenerates `~/.ssh/authorized_keys` from configuration and
> reverts a plain `passwd` at the next commit of `system login`. That platform is a
> Password Safe artifact rather than dashboard code, so it lives outside this
> repository — see
> [the bake's README](https://github.com/Weaverlab-xyz/vm-dashboard-community/blob/main/provisioners/net/README.md)
> for the change command. Until it exists, demo the checkout half and say so; once it
> does, rotation is ordinary Password Safe behaviour.

## The demo, end to end

Roughly fifteen minutes, and the pause in step 4 is the whole point.

1. **Set the scene.** Traffic from a subnet has to be blocked now. Ask the room who
   holds the firewall password today, and how they would get it at 2 a.m.
2. **Request access.** Check the cell's credential out in PRA. Nothing is displayed.
3. **Open the Shell Jump** and make the change, reading it aloud as you go:

   ```bash
   configure
   set firewall ipv4 name BLOCKLIST rule 10 action drop
   set firewall ipv4 name BLOCKLIST rule 10 source address 198.51.100.0/24
   set firewall ipv4 name BLOCKLIST rule 10 description 'Blocked during INC-4471'
   commit
   save
   ```

   Then confirm it is live: `run show firewall ipv4 name BLOCKLIST`.
4. **Stop, and play it back.** Open the session recording. Every keystroke, timestamped,
   with a name against it. This is the moment the demo lands — do not rush past it to
   the next feature.
5. **Let the checkout lapse**, and fail to reconnect with the credential you used.
   (Needs the VyOS platform above; without it the credential stays as issued and this
   step is a talking point rather than a demo.)
6. **Destroy the cell** from the *Instances* tab and show the VM, the Shell Jump and the
   managed system all go together.

On 1.3, substitute `set firewall name BLOCKLIST …` throughout.

## E2E verification checklist

- [ ] **The bake refuses a non-VyOS image.** Point it at a Debian image; it exits naming
      the missing `/opt/vyatta` tooling rather than producing a Debian box with a user on it.
- [ ] **The bake refuses a half-commit.** `/config/config.boot` carries both the account
      and the ruleset, or the build fails.
- [ ] **The deploy refuses a wrong platform.** With the GCP functional account on the
      `GCP VM SSH Rotation` plugin, the deploy 400s naming the remedy.
- [ ] **The deploy refuses without a Gateway**, and without a release train it knows.
- [ ] **The cell has no external IP.**
- [ ] **The Shell Jump connects**, and `configure` enters configuration mode.
- [ ] **The rule survives a reboot** — that is what `save` is for, and it is worth
      showing once.
- [ ] **Destroy removes the VM, the Shell Jump and the managed system.**

## Troubleshooting

**The Shell Jump connects to nothing, or asks for a password you do not have.** The most
likely cause is an image baked with neither `VYOS_ADMIN_PUBKEY` nor
`VYOS_ADMIN_PASSWORD`. VyOS does not create users from a cloud's key injection the way a
stock Linux image does, so that account has no credential at all. The bake warns about
this; re-bake with a key.

**`set firewall …` returns a syntax error.** Wrong release train. Check
`/opt/netcell/IMAGE.txt` on the cell — the bake records what it was built for.

**The commit succeeds but the rule does nothing.** The baseline ruleset was not attached,
or the cell's `VYOS_RULESET` and the deploy form's *baseline ruleset* disagree. Both must
name the same set.

**Password Safe onboarding succeeded but rotation fails.** Expected — see
[What Password Safe does here](#what-password-safe-does-here).

**The deploy 400s naming the functional account's platform.** Working as intended. Point
`passwordsafe_vm_functional_account_gcp` at an SSH-platform account, or deploy with
Password Safe onboarding off.
