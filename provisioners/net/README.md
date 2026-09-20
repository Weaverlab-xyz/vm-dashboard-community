# Network demo cell image (`vyos-cell`)

`vyos-cell.sh` bakes a demo **network device** — a VyOS router/firewall — via the
dashboard's in-app Packer feature. It is the target for the emergency-rule-change
demo: a rep opens a recorded PRA Shell Jump, runs `configure`, adds a drop rule,
`commit`s and `save`s, and the rule is live. Nothing about the CLI, the commit or the
saved configuration is simulated.

See [Network Demo Cell](../../docs/profiles/demo/net-demo-cell.md) for the demo itself.

## You supply the source image

The script configures a VyOS image; it does not build one. **Point the Packer build at
a VyOS source image you already have.** That is deliberate, because the licensing is
not ours to assume:

| Source | Terms |
|---|---|
| VyOS **rolling** release (nightly builds) | Free. The usual choice for a lab. |
| VyOS **LTS** release | Subscription. Use it if you already hold one. |
| VyOS on a cloud marketplace | Paid listing, billed hourly on top of the instance. |

Import your chosen image into the cloud you are baking in, then run this script
against it. The build refuses a non-VyOS image up front (it checks for the
`/opt/vyatta` config tooling) rather than producing a Debian box with a user on it that
the deploy form would happily accept.

Name the resulting image **`vyos-cell`** — that is the name the *Network Cell* tab
expects, exactly as the OT tab expects `ot-sim`.

## Image contract

| What | Where | Notes |
|---|---|---|
| Demo administrator | `VYOS_ADMIN_USER`, default `adminuser` | `level admin`, so it can enter configuration mode. This is the account the cloud's VM deploy path onboards into Password Safe. |
| Its credential | `VYOS_ADMIN_PUBKEY`, or `VYOS_ADMIN_PASSWORD` | Baked in. VyOS does not create users from a cloud's key injection the way a stock Linux image does, so an account baked with neither is an account nothing can log into — the Shell Jump included. The script warns when you bake one anyway. |
| SSH | `:22` | The whole of Layer 1 here. A firewall needs a Shell Jump and nothing else, so the cell provisions no Web Jump and no protocol tunnel. |
| Baseline ruleset | `VYOS_RULESET`, default `BLOCKLIST` | Attached to forwarded traffic, `default-action accept`, and **empty**. The demo's `rule 10 action drop` is the first rule in a ruleset that is already wired in, so the change bites on commit. |
| Hostname | `VYOS_HOSTNAME`, default `vyos-cell` | It is in the prompt, so it is what the audience reads in the recording. |
| Marker | `/opt/netcell/IMAGE.txt` | Answers "is this actually a network cell, baked with what?" during triage, without opening a config session. |
| Break-glass | the image's own `vyos` account | Left untouched on purpose — removing it makes a failed Password Safe onboarding unrecoverable rather than merely broken. |

Everything lands in `/config/config.boot`, a real writable partition on VyOS that
survives image capture. **The configuration is baked, not injected**, and that is a
constraint rather than a preference: the VM deploy paths in this repo have no
user-data hook, so a cell cannot be configured at first boot the way a stock VyOS
cloud image expects. `provisioners/ot/README.md` states the same constraint from the
OT side.

## Build env

| Variable | Default | What it does |
|---|---|---|
| `VYOS_ADMIN_USER` | `adminuser` | The Password-Safe-managed demo account. Matches the OT image's `OT_ADMIN_USER` so both cells onboard under one name. |
| `VYOS_ADMIN_PUBKEY` | *(unset)* | The whole `ssh-ed25519 AAAA… comment` line. Bake the public half of the key pair your PRA Gateway presents. VyOS keeps authorized keys **in configuration** and regenerates `~/.ssh/authorized_keys` from it on commit, so the key has to go in as config or not at all. |
| `VYOS_ADMIN_PASSWORD` | *(unset)* | Initial password, for a Password Safe functional account that rotates passwords rather than keys. |
| `VYOS_HOSTNAME` | `vyos-cell` | Device hostname. |
| `VYOS_RULESET` | `BLOCKLIST` | The ruleset the demo appends to. |
| `VYOS_WAN_IF` | `eth0` | Cloud-facing interface (1.3 syntax attaches the ruleset to it). |
| `VYOS_SYNTAX` | `auto` | `auto`, `1.4` or `1.3`. VyOS reorganised the firewall tree in 1.4 — `firewall ipv4 name` plus a `forward filter` hook, where 1.3 had `firewall name` and per-interface `firewall in`. `auto` picks by looking for the 1.4 template directory, the only reliable on-box signal before a config session exists. |

## How the credential is managed

The cell forces Password Safe's **traditional SSH** onboarding method and refuses to
deploy against a functional account bound to one of the three cloud-native plugins.
That is not a stylistic choice. Those plugins drive the guest through an agent —
SSM on AWS, waagent on Azure, google-guest-agent on GCP — and **VyOS runs none of
them**, so a cell onboarded on a cloud default attaches to a platform that can never
rotate it and looks healthy until the first rotation attempt. `services/netcell_service.py`
carries the guard; `services/ps_vm_hook.py` documents the methods.

> **Not proven on live infrastructure: whether Password Safe can rotate this account.**
> VyOS keeps authorized keys in configuration and regenerates `~/.ssh/authorized_keys`
> from it on every commit of `system login`. Password Safe's SSH method writes that
> file directly, so a rotated key is liable to be reverted by the next commit — and a
> generic Linux platform's change-password command diverges from `config.boot` the same
> way. Managing a VyOS account properly needs a Password Safe platform whose change
> command is `configure; set system login user … authentication plaintext-password …;
> commit; save` in vbash. **That is a Password Safe artifact, not dashboard code**, and
> per `CONTRIBUTING.md` it is not this repo's to ship.
>
> So treat Layer 2 here as **storage and checkout** — the credential is vaulted, handed
> out and never seen — and do not demo rotation until you have verified it against your
> own image and platform. The cell is deployed with auto-management left to your
> functional account's platform rather than forced on.

## Baking it

1. **Images → Build** in the dashboard, source image = your imported VyOS image.
2. Paste `vyos-cell.sh` as the provisioner script.
3. Set any build env from the table above.
4. Name the output image `vyos-cell`.

The script self-elevates with `sudo -E` (Packer invokes the provisioner as the image's
default user, `vyos`) and applies its configuration under `sg vyattacfg` — a VyOS
config session needs that group even when the caller is root, and without it `my_set`
writes into a scratch tree with nothing to commit. It fails loudly if the ruleset or
the account is missing from `/config/config.boot` afterwards, so a silent half-bake
cannot reach the deploy form.
