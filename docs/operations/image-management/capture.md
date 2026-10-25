# Capturing an image from a running instance

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you have a VM configured the way you want and want an image of it.

Part of [Image Management](../image-management.md).

The "I have a VM I've been hand-tuning, snapshot it as an image" path.
Every deploy form has a "Create image" action; the dashboard:

1. Stops the instance (or doesn't, depending on the cloud's snapshot
   semantics — AWS allows live snapshot, Azure requires deallocate +
   generalize).
2. Calls the cloud's native image-creation API.
3. Records the resulting image ID against the source-instance job.

Useful for one-offs but not the recommended steady-state path —
captured images are harder to reproduce than Packer-built ones, and
the build manifest is "whatever was on this VM at this moment", which
ages poorly.
