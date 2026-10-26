# Standard POV images

> **Audience:** operator · **Profile:** `pov` · **Read this when:** you need the Standard Linux or Standard Windows image a cloud template boots.

Part of [Running POVs on a public cloud](../public-cloud.md).

A cloud template names an image for each VM. **POV → Templates → Images** builds the two
images most templates need, in the POV's own cloud account, and adds them to the image
catalog so the template editor can pick them:

| Image | Base | Use it for |
|---|---|---|
| **Standard Linux** | Ubuntu 24.04 LTS | The control node, and Linux targets for Password Safe, PRA Shell Jump and the Entitle agent |
| **Standard Windows** | Windows Server 2022 | The Resource Broker host, and Windows targets for PRA RDP and Password Safe |

Available when the POV cloud is **AWS** or **Azure**. On another POV cloud, give each
template VM an image id directly.

| | AWS | Azure |
|---|---|---|
| Linux base | Newest Ubuntu 24.04 AMI | Marketplace `Canonical:ubuntu-24_04-lts:server` (Gen2) |
| Windows base | Newest Windows Server 2022 AMI | Marketplace `MicrosoftWindowsServer:WindowsServer:2022-datacenter-g2` (Gen2) |
| Build machine | `t3.medium` / `t3.large` | `Standard_B2s` / `Standard_D2s_v3` |
| Result | An AMI | A managed image in the resource group `pov-images-<location>` |
| Windows finisher | EC2Launch `sysprep` | `Sysprep.exe /generalize` |
| Linux login | `adminuser`, given the POV's password by cloud-init | `povadmin`, the account every Azure POV VM is created with |

## Building one

Pick the recipe's card, set the **region** (blank uses the POV cloud's default region), and
choose **Build**. The build is an ordinary job: it starts one temporary instance, runs the
recipe's scripts on it, saves the result as an image (an AMI on AWS, a managed image on
Azure) and deletes the instance. Allow 15–25 minutes for Linux and 30–40 for Windows.
**Live output** opens the job page.

**Build in the region your templates use.** An image exists only in the region (Azure:
location) it was built in. A POV built in a different region is refused with a message that
names both regions, and the template editor warns you about this before you save. To support
two regions, build the image in each.

A finished build is registered in the catalog straight away, with no VHD export, S3 bucket,
Azure storage account or Compute Gallery needed. If registration fails, the job fails too,
because an image that no template can pick is not a finished build.

On Azure the build runs in, and leaves its image in, a resource group named
`pov-images-<location>` (for example `pov-images-eastus`), which the build creates the first
time. The dashboard's Azure identity needs to be able to create resource groups in the
subscription, which a POV already requires because every Azure POV gets its own.

### Options

| Option | Recipe | What it does |
|---|---|---|
| Install the EPM-L package | Linux | Installs the EPM for Linux `.deb` synced to storage (**Storage → BeyondTrust EPM-L packages → Sync to storage**). It is installed but not activated, because activation needs a per-POV installation token |
| Pre-pull the control-node containers | Linux | Bakes in the dashboard agent, PRA Gateway and Ansible runner images so a control node's first boot is quicker. On by default |
| Stage an installer | Windows | Downloads one `.exe`, `.msi` or `.zip` from storage into `C:\BT\staged\` and does **not** run it. Use it for EPM for Windows, or anything else you will install per POV |
| Extra script from storage | both | A `.sh` (Linux) or `.ps1` (Windows) of your own. On Linux it runs before the final sealing step; on Windows it runs after the recipe's scripts and before the restart |

The storage options need the `storage:read` permission and the asset-scope rules the
Storage page uses. Without that permission they are greyed out.

## What each image carries

**Standard Linux** (`provisioners/pov/pov-ready-linux.sh`, then
`provisioners/beyondtrust/bt-ready-debian.sh`):

- Docker CE and the compose plugin, and cloud-init left enabled. That covers what a
  [control node](control-node.md) needs.
- `adminuser`, the account Password Safe manages and Entitle connects as, with full
  passwordless sudo. Entitle's k3s install and Configuration Management `become` both need
  it.
- SSH password login for **one** account: `adminuser` on AWS, `povadmin` on Azure (see
  [The Standard Linux login](#the-standard-linux-login)). Every other account is key-only, as
  PRA Shell Jump expects.
- The PRA Shell Jump sshd prerequisites, plus a clean seal: no host keys, machine-id,
  cloud-init state or leftover build keys.

**Standard Windows** (`provisioners/beyondtrust/bt-ready-windows.ps1`, then
`provisioners/pov/pov-ready-windows.ps1`, then EC2Launch `sysprep` on AWS or
`Sysprep.exe /generalize` on Azure):

- WinRM on 5985, open to private address ranges on every firewall profile. The Resource
  Broker install, the functional account and guest steps all reach the VM this way, and AWS
  POV targets get no user-data that could turn it on later.
- `LocalAccountTokenFilterPolicy = 1`, so the POV's functional account (a local
  administrator) gets a full token over WinRM.
- OpenSSH, and RDP with NLA.
- On AWS, EC2Launch re-armed, so every instance generates its own Administrator password.
  The POV reads that password with its own key, exactly as it does on a stock AMI. On Azure,
  each VM is created with `povadmin` and the POV's generated password, as on a marketplace
  image.
- A boot task, `BT-POV-WinRM`, that re-applies the WinRM settings on every boot and removes
  what the build needed: the build's HTTPS listener and certificate, Basic auth, and the
  build's firewall rule.

**Neither image** holds anything specific to a tenant or a POV: no enrolment code, Gateway
key, Resource Broker install key, Entitle token or password. Those still arrive after boot
through the agent, as they do on any image.

## The Standard Linux login

**On Azure** there is nothing extra: every VM in an Azure POV, on any image, is created with
`povadmin` and the POV's one generated password, and that is the login the VMs tab, the
Resource Broker and the Entitle agent install already use. The Azure Standard Linux image
allows SSH password login for `povadmin` only. Without that, the image's key-only sshd
settings would refuse the platform login. `adminuser` is still created on Azure as the
account for Password Safe to manage, but it has no password until Password Safe sets one.

**On AWS** a target that boots **Standard Linux through the catalog** gets a login on its
first boot.
Each POV has one generated password, stored encrypted in config. cloud-init sets it on
`adminuser`, and the VMs tab, the Resource Broker and the Entitle agent install read it the
same way they read a Windows password. You do not need to type a login into the VMs tab for
these guests. A login you do type still takes precedence.

The password is held in that instance's user-data, so a process on the guest could read it
from the metadata service. Azure's generated password has the same exposure. Once Password
Safe has onboarded `adminuser`, rotate it.

A target that names a literal image id gets no login, even if that AMI was built from the
standard recipe. The dashboard only knows what an image contains when the image comes from
its catalog.

## The catalog

The **Image catalog** table lists every catalog image usable on this cloud, whether it was
built here or promoted here. **Remove** deletes the catalog entry only. The image stays in
the account, still billing for its storage, until you delete it in the cloud console (on AWS,
deregister the AMI; on Azure, delete the managed image from `pov-images-<location>`). Remove
is refused while any cloud template still uses the image.
