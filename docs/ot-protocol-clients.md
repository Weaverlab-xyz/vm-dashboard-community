# OT protocol clients on Windows

How to set up a Windows laptop to read an [OT Demo Cell](cloud-ot.md) through its PRA
protocol tunnels, and what to type in front of a customer. Four Python clients cover
the four protocols the cell simulates — `pymodbus`, `asyncua`, `pylogix` and
`python-snap7` — and all four install as plain wheels with no native toolchain, no
vendor runtime and no licence.

If you would rather demo with a GUI, [UaExpert and QModMaster](#gui-clients-optional)
are covered at the end. Start here regardless: the scripted check is what tells you
the cell is healthy *before* you share your screen.

## What you actually need

| | Why |
|---|---|
| **PRA representative console** | The only thing that creates the tunnel. Download it from your appliance's `/login` → My Account. A Protocol Tunnel Jump listens on `127.0.0.1:<local port>` **only while the session is running**; closing it closes the listener |
| **Python 3.9+** | python.org or the Microsoft Store. `py --version` should answer |
| **Four pip packages** | Below. ~20 MB total, no admin rights needed |

You do **not** need Docker, WSL, admin rights, or a vendor SDK. Unlike Linux, Windows
does not reserve ports below 1024, so the tunnels on `:102` and `:502` bind as a
normal user.

## Install

In PowerShell:

```bash
py -m venv $HOME\ot-clients
& $HOME\ot-clients\Scripts\python.exe -m pip install --upgrade pip
& $HOME\ot-clients\Scripts\python.exe -m pip install "pymodbus>=3.6.8" "asyncua>=2.0.1" pylogix "python-snap7>=3.1.2"
```

Calling `python.exe` by full path skips `Activate.ps1`, which needs an execution-policy
change on a locked-down build. If you would rather activate the venv once per shell:

```bash
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned; & $HOME\ot-clients\Scripts\Activate.ps1
```

Check it took:

```bash
& $HOME\ot-clients\Scripts\python.exe -c "import pymodbus, asyncua, pylogix, snap7; print('ready')"
```

**Behind a TLS-inspecting proxy** (Zscaler, Netskope, Palo Alto — the same one that
breaks Docker pulls), pip fails with `CERTIFICATE_VERIFY_FAILED`. Point it at your
corporate root CA rather than disabling verification:

```bash
$env:PIP_CERT = "C:\path\to\corp-root.crt"
```

The same PEM you would drop in [corp-ca/](../corp-ca/README.md).

### Version notes that matter

- **`asyncua` must be 2.0.1 or newer.** 1.1.x dies on recent CPython in
  `ua_binary.create_type_serializer` (`issubclass() arg 1 must be a class`). The cell
  *serves* OPC UA on 1.1.5 quite happily — this pin is about your machine, not the
  cell.
- **`python-snap7` must be 3.x.** Its wheel bundles the `snap7` binary, so there is no
  DLL to hunt down. (The S7 *server* on the cell is pure Python from 3.0; the client
  side you run here is still a binding to that bundled library.)
- **`pymodbus` renamed the unit-id argument twice** across 3.x — `unit` → `slave` →
  `device_id`. If you copy a snippet off the internet and get a `TypeError`, that is
  why. The examples below use the current spelling.

## Verify the whole cell in one command

[`scripts/ot/verify_tunnels.py`](../scripts/ot/verify_tunnels.py) reads all four
protocols and tells you which are live. Run it **after** starting the cell's tunnel
jump items in the representative console:

```bash
& $HOME\ot-clients\Scripts\python.exe scripts\ot\verify_tunnels.py
```

```
OK   Modbus TCP       127.0.0.1:502   Counter=216  Temperature=42.4C  Flow=104  Running=yes
OK   Siemens S7comm   127.0.0.1:102   Counter=279  Temperature=39.3C  Flow=144  Running=yes
OK   EtherNet/IP      127.0.0.1:44818 Counter=267  Temperature=37.8C  Flow=132  Running=yes
OK   OPC UA           127.0.0.1:4840  Counter=279  Temperature=39.4C  Flow=145  Running=yes

4/4 protocol(s) answered with live, moving values.
```

It reads each protocol twice and compares, so `OK` means the values are *moving* — not
merely that something answered. The four statuses map to the four things that actually
go wrong:

| | Means |
|---|---|
| `OK` | Answered, and the counter advanced between samples |
| `WARN` | Answered, but frozen — the sim's updater thread is wedged. The demo will show dead values |
| `FAIL` | Either nothing is listening (tunnel not started, or on a different local port) or something is listening but not speaking the protocol (that sim was not baked, or its container is dead) — the message says which |
| `SKIP` | You have not installed that client library. Not a failure unless you pass `--strict` |

Useful flags: `--only modbus,opcua` to check a subset, `--modbus-port 1502` when a jump
item uses a non-default local port, `--strict` for a pre-demo gate that refuses to pass
on anything it could not prove, and `--json` to script it. Exit code is 0 only when
every selected protocol answered.

## Reading each protocol by hand

Run these with the venv's Python, one tunnel started at a time. Every sim serves the
**same four process values**, so the same demo story works for any vendor in the room:

| Value | Modbus / S7 / EtherNet-IP | OPC UA |
|---|---|---|
| Counter | increments every second, wraps at 65535 | same, as `Int32` |
| Temperature | degrees C **× 10** as an integer (`404` = 40.4 °C) | real degrees C, as a `Double` |
| Flow | ~120 ± 30 | same, as a `Double` |
| Running | always 1 | `true` |

The ×10 is not a bug: fieldbus protocols carry no unit metadata, so scaled integers are
what real gear does. OPC UA is typed, so it carries the engineering value — which is
itself a good thirty seconds of the demo.

### Modbus TCP — `127.0.0.1:502`

Holding registers 0–3, function code 3. Coil 0 also toggles every second.

```python
from pymodbus.client import ModbusTcpClient

client = ModbusTcpClient("127.0.0.1", port=502)
client.connect()
rr = client.read_holding_registers(0, count=4, device_id=1)
print(rr.registers)          # [1234, 404, 131, 1]
client.close()
```

### OPC UA — `127.0.0.1:4840`

Anonymous, no security policy — exactly how most plant-floor OPC UA servers are
actually deployed, which is the point: the *network path* is the control.

```python
import asyncio
from asyncua import Client

async def main():
    url = "opc.tcp://127.0.0.1:4840/freeopcua/server/"
    async with Client(url=url) as client:
        ns = await client.get_namespace_index("http://ot-sim.demo")
        plant = await client.nodes.objects.get_child([f"{ns}:Plant"])
        for name in ("Counter", "Temperature", "Flow", "Running"):
            node = await plant.get_child([f"{ns}:{name}"])
            print(name, await node.read_value())

asyncio.run(main())
```

The endpoint path is `/freeopcua/server/`. `asyncua` ignores a wrong path; stricter
clients refuse the connection, so get it right in any GUI you configure.

`Requested session timeout to be 3600000ms, got 600000ms instead` on stderr is
`asyncua` noting that the server granted a shorter session than it asked for. It is
expected and harmless — `verify_tunnels.py` silences it so it cannot read as a fault
mid-demo.

### EtherNet/IP — `127.0.0.1:44818`

Four `DINT` CIP tags, read with `pylogix` — the client BeyondTrust's OT material points
customers at.

```python
from pylogix import PLC

with PLC() as comm:
    comm.IPAddress = "127.0.0.1"
    for name in ("Counter", "Temperature", "Flow", "Running"):
        ret = comm.Read(name)
        print(name, ret.Status, ret.Value)
```

Tag names are case-sensitive and capitalised exactly as above.

### Siemens S7comm — `127.0.0.1:102`

DB1, four big-endian words at offsets 0/2/4/6. S7 has no tag names — a wrong offset
reads as plausible garbage rather than an error, so keep the `>HHHH`.

```python
import struct, snap7

client = snap7.client.Client()
client.connect("127.0.0.1", 0, 1, tcp_port=102)
print(struct.unpack(">HHHH", bytes(client.db_read(1, 0, 8))))
client.disconnect()
```

`Expected COTP DT, got 0x80` on stderr during connect is harmless noise from the
pure-Python server; the reads still work.

### DNP3 — not simulated

The cell does not serve DNP3. `opendnp3` needs a library built from source, which the
image's every-dependency-is-a-pinned-wheel contract cannot honour, so the DNP3 preset
exists for a **standalone tunnel to real or lab gear**. The deploy form still offers it,
so if you tick DNP3 on a cell you will get a tunnel to a port with nothing behind it —
which looks exactly like a blocked firewall.

## Running the demo

1. **Deploy the cell** and wait for the parent job to finish green (cloud page → OT
   tab). Tick every protocol you intend to show; each becomes its own tunnel jump item.
2. **Before the call**, start the jumps and run `verify_tunnels.py --strict`. This is
   the whole reason it exists — a cell that answers now will answer in ten minutes, and
   a cell that does not gives you time to Re-wire or redeploy.
3. **Open with the HMI.** Web Jump to FUXA on `:1881` needs nothing installed and shows
   the plant moving. Then reveal that the VM has no public IP, no inbound rule, and no
   route off the plant subnet.
4. **Then the protocol.** Start the tunnel jump, run the snippet for whichever vendor
   the customer runs, and read live values off an air-gapped PLC — through a session
   that is recorded and attributable.
5. **Land the point.** The tunnel is generic TCP: no credential is injected on the
   wire, because these protocols are unauthenticated by design in most real PLCs. The
   network path *is* the control, and PRA is the only way in.

One protocol per tunnel jump, and two tunnels cannot listen on the same local port at
once — so if you are switching vendors live, close one session before starting the
next, or give them distinct local ports.

## GUI clients (optional)

Worth installing if you want something more visual than a terminal, but none of them
are required:

| Protocol | Client | Notes |
|---|---|---|
| OPC UA | **UaExpert** (Unified Automation, free with registration) | Add the server as `opc.tcp://127.0.0.1:4840/freeopcua/server/`, anonymous, None/None security. Browse `Objects` → `Plant` |
| Modbus | **QModMaster** (free) or **Modbus Poll** (trial) | Slave 1, holding registers, start 0, quantity 4 |
| Siemens | **TIA Portal** | PLC address `127.0.0.1` — only worth it if the customer already lives in it |

**Point GUI clients at `127.0.0.1` explicitly.** The OPC UA sim advertises its endpoint
as `0.0.0.0`, and some OPC UA and EtherNet/IP stacks reconnect to whatever address the
server advertises rather than the one you typed. The tunnel forwards only the brokered
port, so a client that wanders off it just hangs.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `verify_tunnels.py` says nothing is listening, on every protocol | The tunnel jumps are not started in the representative console, or the session dropped. The listener only exists for the life of the session |
| Nothing listening on **one** protocol | That jump item uses a different local port — check the jump item and pass `--<protocol>-port` |
| Listener answers nothing | That sim was not baked into the image (`OT_SIMS` selects them at bake time; an image baked before Siemens was added has no `ot-s7`), or its container died. Shell Jump to the cell and run `docker ps` |
| Values answer but never change | The sim's updater thread is wedged — `docker restart` the container, or redeploy the cell |
| The tunnel will not bind the local port | Something else holds it: `Get-NetTCPConnection -LocalPort 502`. Hyper-V and WSL also reserve port ranges — `netsh int ipv4 show excludedportrange protocol=tcp`. Give the jump item a different local port and pass it to the script |
| pip fails with `CERTIFICATE_VERIFY_FAILED` | TLS-inspecting proxy — set `PIP_CERT` to your corporate root CA (above) |
| `TypeError` on `read_holding_registers` | pymodbus version drift on the unit-id kwarg — try `slave=1` instead of `device_id=1` |
| OPC UA connects but browsing finds no `Plant` | Wrong namespace or endpoint path. Resolve the index with `get_namespace_index("http://ot-sim.demo")` rather than hardcoding `ns=2` |
| Azure cell: Web Jump works, the tunnel never establishes | The Gateway resolved to an **ACI** jumpoint, which cannot do protocol tunneling. See [cloud-ot.md](cloud-ot.md#troubleshooting) |

## See also

- [OT Demo Cell](cloud-ot.md) — deploying the cell, the PRA wiring, standalone tunnels
  to real gear, and the [tunnel reference](cloud-ot.md#using-the-protocol-tunnels)
- [`provisioners/ot/README.md`](../provisioners/ot/README.md) — what the image contains,
  the version pins, and how to swap in real OpenPLC
