# Design notes

> **Audience:** contributor · **Profile:** `both` · **Read this when:** you are about to change a subsystem and want the reasoning that is not recoverable from its code.

These are not user guides. Each one records why a subsystem is shaped the way it is — the
constraint that forced the shape, the alternatives that were rejected, and the failure the
current design avoids. That is the part a diff cannot tell you, and the part most likely to
be undone by someone who reads only the code.

They are deliberately **not** on the `/docs` index in the app. An operator following a
Settings panel's instruction should land on a guide, not a design argument; these are
reachable by path, and from here.

| Page | Read this when |
|---|---|
| [Machine-identity JIT cloud access](cloud-identity-jit.md) | you are changing how the dashboard's own cloud writes are authorised. |
| [Entitle user-based JIT (Entra)](entitle-user-jit.md) | you are wiring the dashboard into an existing Entra ID and Entitle deployment, or migrating off that path. |
| [Entitle resource registration](entitle-resource-registration.md) | you are changing how built resources register themselves as Entitle integrations. |
| [Cloud Functions](cloud-functions.md) | you are extending the Cloud Functions preview or its workload catalog. |
| [k8s ServiceAccount token rotation](k8s-sa-token-rotation.md) | you are touching ServiceAccount token rotation and need the reasoning that is not in the code. |
| [The dashboard deploys `bt-dbops`](ps-dbops-cloud-run.md) | you are working on the in-VPC service the Cloud SQL rotation plugin calls. |

Two more design notes sit with the profile they belong to, in
[profiles/pov/design/](../profiles/pov/design/README.md).

The procedures that prove a design's phases actually landed are in
[runbooks/](../runbooks/README.md); several of them are numbered against the phases in
[cloud-identity-jit.md](cloud-identity-jit.md).
