# Runbooks

> **Audience:** contributor · **Profile:** `both` · **Read this when:** you need to prove a piece of work actually functions against a real instance, not just in tests.

A runbook is a procedure you execute, in order, against a live instance and real cloud
accounts — the step, the command, and what a pass looks like. They exist because the
things they cover cannot be unit-tested: they need a real Entitle tenant, a real cloud
account, or a real appliance.

Most are numbered against the phases of a design note, so the number is the order the work
landed rather than a difficulty rating. They are kept after the phase ships: the procedure
is also how you diagnose that phase later.

Like [design/](../design/README.md), these are off the `/docs` index in the app and
reachable by path.

## One-time operator setup

| Page | Read this when |
|---|---|
| [Cloud-DB Password Safe plugin setup](clouddb-password-safe-plugin-setup.md) | you are standing up database credential rotation and need the one-time manual setup. |

## Cloud-identity JIT

Phases of [design/cloud-identity-jit.md](../design/cloud-identity-jit.md).

| Page | Read this when |
|---|---|
| [Phase 0 — smoke test](cloud-identity-jit-phase-0-smoke-test.md) | you are verifying the cloud-identity-JIT scaffolding before wiring anything real. |
| [Phase 1 — Entitle submit + poll](cloud-identity-jit-phase-1-entitle-submit.md) | you are verifying that a real elevation request reaches Entitle. |
| [Phase 2 — AWS EC2 deploy + terminate](cloud-identity-jit-phase-2-aws-deploy-wrapped.md) | you are verifying the first cloud SDK writes wrapped in an elevation. |
| [Phase 4a — AWS sweeper](cloud-identity-jit-phase-4a-aws-sweeper.md) | you are verifying the AWS reconciliation sweeper. |
| [Phase 4b — Azure sweeper](cloud-identity-jit-phase-4b-azure-sweeper.md) | you are verifying the Azure reconciliation sweeper. |
| [Phase 4c — GCP sweeper](cloud-identity-jit-phase-4c-gcp-sweeper.md) | you are verifying the GCP reconciliation sweeper. |

## Entitle user JIT

Phases of [design/entitle-user-jit.md](../design/entitle-user-jit.md), the legacy
Entra-group path.

| Page | Read this when |
|---|---|
| [Phase 0 — resolver verification](entitle-user-jit-phase-0-resolver.md) | you are verifying how the legacy Entra-group resolver behaves. |
| [Phase 1 — bootstrap Entra groups](entitle-user-jit-phase-1-bootstrap-entra.md) | you are creating the Entra groups the legacy user-JIT path needs. |
| [Phase 2 — bootstrap Entitle applications](entitle-user-jit-phase-2-bootstrap-entitle.md) | you are provisioning the Entitle virtual applications that back those groups. |

## Other end-to-end checks

| Page | Read this when |
|---|---|
| [First Cloud Functions deploy](cloud-functions-first-deploy.md) | you are bringing Cloud Functions up end to end for the first time. |
| [Entitle resource registration (E2E)](entitle-resource-registration.md) | you want to confirm built resources really do register as Entitle integrations. |
