"""
Docker Compose → provider-agnostic spec parser/validator.

The Containers page lets users deploy a Docker Compose file (referenced from the
storage backend) to ECS / ACI / GCE. Those runtimes build multi-container units
natively, but each speaks its own API — so we first parse the compose YAML into a
small, normalized spec the per-provider deploy functions translate from.

Scope is deliberately a **core subset** (image, command, environment, ports,
restart, CPU/memory limits). Anything outside that subset is *rejected* with a
clear error rather than silently dropped, so a partial translation is never
deployed. A catalog / richer compose support is deferred to the SaaS product.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Optional


class ComposeError(Exception):
    """Raised when a compose file can't be parsed or uses an unsupported feature."""


# Top-level keys we understand. `version`/`name` are accepted and ignored;
# `x-*` extension keys are ignored. Everything else is rejected.
_ALLOWED_TOP_KEYS = {"version", "name", "services"}

# Per-service keys we understand. `container_name` is accepted and ignored
# (the service name is used). `deploy` is accepted but only for resource limits.
_ALLOWED_SERVICE_KEYS = {
    "image", "entrypoint", "command", "environment", "ports", "restart", "deploy",
    "container_name",
}

# Restart policies compose allows; mapped to each provider downstream.
_VALID_RESTART = {"no", "always", "on-failure", "unless-stopped"}


@dataclass
class ComposeService:
    name: str
    image: str
    entrypoint: Optional[list[str]] = None  # overrides image ENTRYPOINT
    command: Optional[list[str]] = None      # overrides image CMD
    env: list[tuple[str, str]] = field(default_factory=list)
    # (host_port, container_port, protocol)
    ports: list[tuple[int, int, str]] = field(default_factory=list)
    cpu: Optional[float] = None          # vCPU fraction, e.g. 0.5
    memory_mb: Optional[int] = None      # mebibytes
    restart: Optional[str] = None        # one of _VALID_RESTART


@dataclass
class ComposeSpec:
    services: list[ComposeService]


def parse_and_validate(yaml_text: str) -> ComposeSpec:
    """Parse compose YAML into a ComposeSpec, raising ComposeError on anything
    outside the supported subset."""
    try:
        import yaml
        data = yaml.safe_load(yaml_text)
    except Exception as e:
        raise ComposeError(f"Could not parse compose YAML: {e}") from e

    if not isinstance(data, dict):
        raise ComposeError("Compose file must be a YAML mapping at the top level.")

    bad_top = [
        k for k in data
        if k not in _ALLOWED_TOP_KEYS and not str(k).startswith("x-")
    ]
    if bad_top:
        raise ComposeError(
            "Unsupported top-level compose key(s): "
            f"{', '.join(sorted(map(str, bad_top)))}. "
            "Only 'services' (plus 'version'/'name') are supported."
        )

    services = data.get("services")
    if not isinstance(services, dict) or not services:
        raise ComposeError("Compose file has no 'services'.")

    parsed = [_parse_service(name, body) for name, body in services.items()]
    return ComposeSpec(services=parsed)


def _parse_service(name: str, body: dict) -> ComposeService:
    if not isinstance(body, dict):
        raise ComposeError(f"Service '{name}' must be a mapping.")

    bad = [
        k for k in body
        if k not in _ALLOWED_SERVICE_KEYS and not str(k).startswith("x-")
    ]
    if bad:
        raise ComposeError(
            f"Service '{name}' uses unsupported key(s): "
            f"{', '.join(sorted(map(str, bad)))}. Supported: image, command, "
            "environment, ports, restart, deploy.resources.limits "
            "(cpus/memory)."
        )

    image = body.get("image")
    if not image or not isinstance(image, str):
        raise ComposeError(f"Service '{name}' must specify an 'image'.")

    restart = body.get("restart")
    if restart is not None and restart not in _VALID_RESTART:
        raise ComposeError(
            f"Service '{name}' has unsupported restart policy '{restart}'. "
            f"One of: {', '.join(sorted(_VALID_RESTART))}."
        )

    cpu, memory_mb = _parse_resources(name, body.get("deploy"))

    return ComposeService(
        name=str(name),
        image=image,
        entrypoint=_parse_command(name, body.get("entrypoint"), "entrypoint"),
        command=_parse_command(name, body.get("command")),
        env=_parse_env(name, body.get("environment")),
        ports=_parse_ports(name, body.get("ports")),
        cpu=cpu,
        memory_mb=memory_mb,
        restart=restart,
    )


def _parse_command(name: str, command, field: str = "command") -> Optional[list[str]]:
    if command is None:
        return None
    if isinstance(command, str):
        return shlex.split(command)
    if isinstance(command, list):
        return [str(c) for c in command]
    raise ComposeError(f"Service '{name}' has an invalid '{field}' (expected string or list).")


def _parse_env(name: str, environment) -> list[tuple[str, str]]:
    if environment is None:
        return []
    out: list[tuple[str, str]] = []
    if isinstance(environment, dict):
        for k, v in environment.items():
            out.append((str(k), "" if v is None else str(v)))
        return out
    if isinstance(environment, list):
        for item in environment:
            s = str(item)
            if "=" in s:
                k, v = s.split("=", 1)
                out.append((k, v))
            else:
                # "KEY" with no value — compose pulls it from the host env, which
                # doesn't exist on a remote runtime. Reject rather than guess.
                raise ComposeError(
                    f"Service '{name}' environment entry '{s}' has no value. "
                    "Host-passthrough env vars aren't supported; use KEY=VALUE."
                )
        return out
    raise ComposeError(f"Service '{name}' has an invalid 'environment' (expected map or list).")


def _parse_ports(name: str, ports) -> list[tuple[int, int, str]]:
    if ports is None:
        return []
    if not isinstance(ports, list):
        raise ComposeError(f"Service '{name}' has an invalid 'ports' (expected a list).")

    out: list[tuple[int, int, str]] = []
    for entry in ports:
        # Long-form mapping: {target: 80, published: 8080, protocol: tcp}
        if isinstance(entry, dict):
            target = entry.get("target")
            published = entry.get("published", target)
            proto = str(entry.get("protocol", "tcp")).lower()
            if target is None:
                raise ComposeError(f"Service '{name}' port mapping is missing 'target'.")
            out.append((_as_port(name, published), _as_port(name, target), _check_proto(name, proto)))
            continue

        s = str(entry)
        if "-" in s.split("/")[0]:
            raise ComposeError(
                f"Service '{name}' port range '{s}' isn't supported; list ports individually."
            )
        proto = "tcp"
        if "/" in s:
            s, proto = s.rsplit("/", 1)
            proto = proto.lower()
        parts = s.split(":")
        # forms: "container", "host:container", "ip:host:container"
        if len(parts) == 1:
            host_p = container_p = parts[0]
        elif len(parts) == 2:
            host_p, container_p = parts
        elif len(parts) == 3:
            host_p, container_p = parts[1], parts[2]
        else:
            raise ComposeError(f"Service '{name}' has an invalid port mapping '{entry}'.")
        out.append((_as_port(name, host_p), _as_port(name, container_p), _check_proto(name, proto)))
    return out


def _check_proto(name: str, proto: str) -> str:
    if proto not in ("tcp", "udp"):
        raise ComposeError(f"Service '{name}' has unsupported port protocol '{proto}'.")
    return proto


def _as_port(name: str, value) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        raise ComposeError(f"Service '{name}' has a non-numeric port '{value}'.")
    if not (1 <= port <= 65535):
        raise ComposeError(f"Service '{name}' has an out-of-range port '{value}'.")
    return port


def _parse_resources(name: str, deploy) -> tuple[Optional[float], Optional[int]]:
    """Read deploy.resources.limits.{cpus,memory}. Reject other deploy keys so
    orchestration directives (replicas, placement, …) never look honored."""
    if deploy is None:
        return None, None
    if not isinstance(deploy, dict):
        raise ComposeError(f"Service '{name}' has an invalid 'deploy' block.")
    extra = [k for k in deploy if k != "resources"]
    if extra:
        raise ComposeError(
            f"Service '{name}' deploy key(s) not supported: {', '.join(sorted(map(str, extra)))}. "
            "Only deploy.resources.limits is supported."
        )
    resources = deploy.get("resources") or {}
    if not isinstance(resources, dict):
        raise ComposeError(f"Service '{name}' has an invalid 'deploy.resources' block.")
    extra = [k for k in resources if k != "limits"]
    if extra:
        raise ComposeError(
            f"Service '{name}' deploy.resources key(s) not supported: "
            f"{', '.join(sorted(map(str, extra)))}. Only 'limits' is supported."
        )
    limits = resources.get("limits") or {}
    if not isinstance(limits, dict):
        raise ComposeError(f"Service '{name}' has an invalid 'deploy.resources.limits' block.")

    cpu = None
    if "cpus" in limits:
        try:
            cpu = float(limits["cpus"])
        except (TypeError, ValueError):
            raise ComposeError(f"Service '{name}' has an invalid cpus limit '{limits['cpus']}'.")
    memory_mb = _parse_memory(name, limits.get("memory")) if "memory" in limits else None
    return cpu, memory_mb


def _parse_memory(name: str, value) -> Optional[int]:
    """Parse a compose memory string ('512M', '1g', '1073741824') into MiB."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    # Tolerate a trailing 'b' (e.g. "512mb", "100b") before reading the unit char.
    if s.endswith("b"):
        s = s[:-1]
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    mult = 1
    if s and s[-1] in units:
        mult = units[s[-1]]
        s = s[:-1]
    try:
        raw_bytes = float(s) * mult
    except ValueError:
        raise ComposeError(f"Service '{name}' has an invalid memory limit '{value}'.")
    return max(1, int(raw_bytes / (1024**2)))
