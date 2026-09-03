#!/usr/bin/env python3
"""Read the OT demo cell's simulators through their PRA protocol tunnels.

Run this on the rep's machine with the cell's Protocol Tunnel Jumps started in the
PRA representative console. Each tunnel listens on ``127.0.0.1:<local port>``, so
every check below is a plain localhost client -- the same clients docs/profiles/demo/ot-demo-cell.md
tells customers to use (pymodbus, python-snap7, pylogix, asyncua).

Why it exists: the bake's smoke test only asserts ``.State.Running``, so a cell can
boot with a container up and its listener dead, and a tunnel to a dead port is
indistinguishable from a blocked firewall once you are standing in front of a
customer. This separates the three failure modes explicitly:

  FAIL  no listener      the tunnel jump is not started, or is on another local port
  FAIL  listener, no protocol answer   sim not baked / container dead (OT_SIMS)
  WARN  answers but static            the sim's updater thread is wedged
  OK    answers and the counter moves between samples

Each protocol is optional: an uninstalled client library is a SKIP, not a crash, so
``python verify_tunnels.py --only modbus`` works with nothing but pymodbus present.

  pip install "pymodbus>=3.6.8" "asyncua>=2.0.1" pylogix "python-snap7>=3.1.2"

Usage:
  python scripts/ot/verify_tunnels.py
  python scripts/ot/verify_tunnels.py --only modbus,opcua
  python scripts/ot/verify_tunnels.py --modbus-port 1502 --s7-port 1102
  python scripts/ot/verify_tunnels.py --json

Exit code is 0 when every selected protocol answered, 1 when any FAILed. ``--strict``
also fails on WARN and SKIP, which is what you want in a pre-demo check: a SKIP means
you never actually proved that protocol.

DNP3 (:20000) is deliberately absent -- the cell does not simulate it (opendnp3 needs
a source build, which the image's pinned-wheel contract cannot honour). A DNP3 preset
is a standalone tunnel to real gear.
"""
import argparse
import json
import socket
import struct
import sys
import time

# The cell's contract, from provisioners/ot/ot-sim-debian.sh. tests/
# test_ot_client_contract.py holds these against the simulator sources, so a sim
# that moves a port or renames a tag fails there rather than here, mid-demo.
DEFAULT_PORTS = {
    "modbus": 502,
    "s7": 102,
    "enip": 44818,
    "opcua": 4840,
}
OPCUA_PATH = "/freeopcua/server/"
OPCUA_NAMESPACE = "http://ot-sim.demo"
# Every sim serves these four values, in this order, so one demo script reads the
# same "plant" through any vendor's protocol.
VALUES = ("Counter", "Temperature", "Flow", "Running")
LABELS = {
    "modbus": "Modbus TCP",
    "s7": "Siemens S7comm",
    "enip": "EtherNet/IP",
    "opcua": "OPC UA",
}
PIP_NAMES = {
    "modbus": "pymodbus>=3.6.8",
    "s7": "python-snap7>=3.1.2",
    "enip": "pylogix",
    "opcua": "asyncua>=2.0.1",
}

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"


class Skipped(Exception):
    """The client library for this protocol is not installed."""


# -- protocol readers ----------------------------------------------------------
# Each returns {"Counter": int, "Temperature": float, "Flow": float,
# "Running": bool}. Temperature is normalised to real degrees C: the fieldbus
# protocols carry it x10 as an integer (no unit metadata), OPC UA carries a Double.


def read_modbus(host, port, timeout):
    try:
        import logging

        from pymodbus.client import ModbusTcpClient
    except ImportError as exc:
        raise Skipped(str(exc))

    # pymodbus logs its retry exhaustion at ERROR and then raises the same text,
    # so an unconfigured process prints the failure twice, in two voices, above
    # this script's own account of it. A NullHandler is what silences it: raising
    # the level cannot (the message IS at ERROR), and without any handler in the
    # chain logging falls back to lastResort, which writes to stderr.
    logging.getLogger("pymodbus").addHandler(logging.NullHandler())

    client = ModbusTcpClient(host, port=port, timeout=timeout)
    if not client.connect():
        raise RuntimeError("pymodbus could not open the connection")
    try:
        # pymodbus renamed the unit-id kwarg twice across 3.x (unit -> slave ->
        # device_id) and made count keyword-only at 3.7. The sim's context is
        # single=True, so any unit id is accepted; only the spelling matters.
        rr = None
        errors = []
        for kwargs in ({"slave": 1}, {"device_id": 1}, {}):
            try:
                rr = client.read_holding_registers(0, count=4, **kwargs)
                break
            except TypeError as exc:
                errors.append("%s: %s" % (kwargs or "no unit kwarg", exc))
        if rr is None:
            raise RuntimeError("no read_holding_registers signature matched (%s)"
                               % "; ".join(errors))
        if rr.isError():
            raise RuntimeError("Modbus exception response: %s" % rr)
        regs = list(rr.registers)
        if len(regs) < 4:
            raise RuntimeError("expected 4 holding registers, got %d" % len(regs))
        return {
            "Counter": regs[0],
            "Temperature": regs[1] / 10.0,
            "Flow": float(regs[2]),
            "Running": bool(regs[3]),
        }
    finally:
        client.close()


def read_s7(host, port, timeout):
    try:
        import snap7
    except ImportError as exc:
        raise Skipped(str(exc))

    client_cls = getattr(getattr(snap7, "client", None), "Client", None)
    if client_cls is None:
        client_cls = getattr(snap7, "Client", None)
    if client_cls is None:
        raise RuntimeError("python-snap7 exposes no Client class")

    client = client_cls()
    # snap7's client is a binding to libsnap7 (the *server* went pure-Python at
    # 3.0, the client did not) -- the wheel bundles the library. The tcp port kwarg
    # was renamed tcpport -> tcp_port at 3.0.
    connected = False
    for kwargs in ({"tcp_port": port}, {"tcpport": port}):
        try:
            client.connect(host, 0, 1, **kwargs)
            connected = True
            break
        except TypeError:
            continue
    if not connected:
        client.connect(host, 0, 1)
    try:
        data = client.db_read(1, 0, 8)
        counter, temperature, flow, running = struct.unpack(">HHHH", bytes(data))
        return {
            "Counter": counter,
            "Temperature": temperature / 10.0,
            "Flow": float(flow),
            "Running": bool(running),
        }
    finally:
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001  (a failed disconnect is not a finding)
            pass


def read_enip(host, port, timeout):
    try:
        from pylogix import PLC
    except ImportError as exc:
        raise Skipped(str(exc))

    comm = PLC()
    comm.IPAddress = host
    comm.SocketTimeout = timeout
    if port != DEFAULT_PORTS["enip"]:
        # pylogix keeps the TCP port on the connection object, not the PLC.
        conn = getattr(comm, "conn", None)
        if conn is None or not hasattr(conn, "Port"):
            raise RuntimeError(
                "this pylogix cannot be pointed at port %d -- give the tunnel jump "
                "local port %d instead" % (port, DEFAULT_PORTS["enip"]))
        conn.Port = port
    try:
        values = {}
        failures = []
        for name in VALUES:
            ret = comm.Read(name)
            if getattr(ret, "Status", None) != "Success" or ret.Value is None:
                failures.append("%s: %s" % (name, getattr(ret, "Status", ret)))
                continue
            values[name] = ret.Value
        if failures:
            raise RuntimeError("CIP read failed for %s" % ", ".join(failures))
        return {
            "Counter": int(values["Counter"]),
            "Temperature": int(values["Temperature"]) / 10.0,
            "Flow": float(values["Flow"]),
            "Running": bool(values["Running"]),
        }
    finally:
        comm.Close()


def read_opcua(host, port, timeout, path=OPCUA_PATH):
    try:
        import asyncio
        import logging

        from asyncua import Client
    except ImportError as exc:
        raise Skipped(str(exc))

    # asyncua warns on stderr about the secure-channel lifetime the server grants
    # and about anonymous/unencrypted endpoints. All three are expected here (the
    # sim is anonymous, NoSecurity, by design) and reading them mid-demo suggests
    # a problem that isn't one. Failures still surface as exceptions below.
    logging.getLogger("asyncua").setLevel(logging.ERROR)

    url = "opc.tcp://%s:%d%s" % (host, port, path)

    async def _read():
        # The sim advertises its endpoint as 0.0.0.0; asyncua keeps talking to the
        # URL you gave it, so the tunnel is followed rather than the advertised
        # address. Clients that chase the advertised host (UaExpert) need to be
        # pointed at 127.0.0.1 explicitly.
        client = Client(url=url, timeout=timeout)
        await client.connect()
        try:
            idx = await client.get_namespace_index(OPCUA_NAMESPACE)
            plant = await client.nodes.objects.get_child(["%d:Plant" % idx])
            out = {}
            for name in VALUES:
                node = await plant.get_child(["%d:%s" % (idx, name)])
                out[name] = await node.read_value()
            return out
        finally:
            await client.disconnect()

    raw = asyncio.run(_read())
    return {
        "Counter": int(raw["Counter"]),
        # OPC UA is typed, so this face carries real degrees C rather than the
        # fieldbus x10 integer. Do not divide it again.
        "Temperature": float(raw["Temperature"]),
        "Flow": float(raw["Flow"]),
        "Running": bool(raw["Running"]),
    }


READERS = {
    "modbus": read_modbus,
    "s7": read_s7,
    "enip": read_enip,
    "opcua": read_opcua,
}


# -- checking ------------------------------------------------------------------


def port_open(host, port, timeout):
    """Is anything listening? Separates 'tunnel not started' from 'sim dead'."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check(key, host, port, timeout, samples, gap, opcua_path):
    reader = READERS[key]
    kwargs = {"path": opcua_path} if key == "opcua" else {}

    if not port_open(host, port, timeout):
        return {
            "status": FAIL,
            "detail": "nothing is listening on %s:%d -- start the %s tunnel jump in "
                      "the PRA representative console (or pass --%s-port if its "
                      "local port differs)" % (host, port, LABELS[key], key),
            "values": None,
        }

    readings = []
    for i in range(max(1, samples)):
        if i:
            time.sleep(gap)
        try:
            readings.append(reader(host, port, timeout, **kwargs))
        except Skipped as exc:
            return {
                "status": SKIP,
                "detail": "client library not installed (pip install %s) [%s]"
                          % (PIP_NAMES[key], exc),
                "values": None,
            }
        except Exception as exc:  # noqa: BLE001  (any client error is a finding)
            return {
                "status": FAIL,
                "detail": "%s:%d accepted the connection but did not answer %s: "
                          "%s: %s -- the sim is probably not baked into this image "
                          "(OT_SIMS) or its container is dead"
                          % (host, port, LABELS[key], type(exc).__name__, exc),
                "values": None,
            }

    latest = readings[-1]
    if len(readings) > 1 and readings[0]["Counter"] == latest["Counter"]:
        return {
            "status": WARN,
            "detail": "answered, but Counter did not move in %.1fs (%d) -- the sim's "
                      "updater thread is wedged; the demo will show frozen values"
                      % (gap, latest["Counter"]),
            "values": latest,
        }
    return {"status": OK, "detail": "", "values": latest}


def format_values(values):
    if not values:
        return ""
    return "Counter=%d  Temperature=%.1fC  Flow=%.0f  Running=%s" % (
        values["Counter"], values["Temperature"], values["Flow"],
        "yes" if values["Running"] else "no")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read the OT demo cell's simulators through their PRA tunnels.",
        epilog="Start the tunnel jump items in the PRA representative console first.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="tunnel listen address (default: 127.0.0.1)")
    for key, port in DEFAULT_PORTS.items():
        parser.add_argument("--%s-port" % key, type=int, default=port,
                            help="local port for %s (default: %d)"
                                 % (LABELS[key], port))
    parser.add_argument("--opcua-path", default=OPCUA_PATH,
                        help="OPC UA endpoint path (default: %s)" % OPCUA_PATH)
    parser.add_argument("--only", default="",
                        help="comma-separated subset of: %s"
                             % ", ".join(DEFAULT_PORTS))
    parser.add_argument("--timeout", type=float, default=5.0,
                        help="per-operation timeout in seconds (default: 5)")
    parser.add_argument("--samples", type=int, default=2,
                        help="reads per protocol; 2+ proves the values tick "
                             "(default: 2)")
    parser.add_argument("--gap", type=float, default=1.5,
                        help="seconds between samples (default: 1.5)")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero on WARN and SKIP too")
    args = parser.parse_args(argv)

    keys = list(DEFAULT_PORTS)
    if args.only:
        keys = [k.strip().lower() for k in args.only.split(",") if k.strip()]
        unknown = [k for k in keys if k not in DEFAULT_PORTS]
        if unknown:
            parser.error("unknown protocol(s): %s (known: %s)"
                         % (", ".join(unknown), ", ".join(DEFAULT_PORTS)))

    results = {}
    for key in keys:
        port = getattr(args, "%s_port" % key)
        result = check(key, args.host, port, args.timeout, args.samples, args.gap,
                       args.opcua_path)
        result["port"] = port
        result["label"] = LABELS[key]
        results[key] = result
        if not args.json:
            line = "%-4s %-16s %s:%-5d %s" % (
                result["status"], LABELS[key], args.host, port,
                format_values(result["values"]) or result["detail"])
            print(line)
            if result["values"] and result["detail"]:
                print("     %s" % result["detail"])

    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))

    statuses = [r["status"] for r in results.values()]
    bad = [s for s in statuses if s == FAIL]
    if args.strict:
        bad += [s for s in statuses if s in (WARN, SKIP)]
    if not args.json:
        print()
        answered = sum(1 for s in statuses if s == OK)
        static = sum(1 for s in statuses if s == WARN)
        summary = ("%d/%d protocol(s) answered with live, moving values."
                   % (answered, len(statuses)))
        if static:
            summary += " %d answered but was frozen." % static
        print(summary)
        if any(s == FAIL for s in statuses):
            print("A FAIL on every protocol usually means the tunnels are not "
                  "started; a FAIL on one means that sim is missing or dead "
                  "(Shell Jump in and run: docker ps).")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
