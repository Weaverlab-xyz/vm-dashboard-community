# Browsing images

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want to see which images each cloud has, and which of them the dashboard built.

Part of [Image Management](../image-management.md).

The per-cloud pages list both your private images (account-scoped)
and curated public catalogues:

| Cloud | Private | Public |
|---|---|---|
| AWS | Your account's AMIs (region-scoped) | A curated allow-list of well-known AMI publishers (Amazon Linux, Ubuntu, Debian) |
| Azure | Managed Images + Shared Image Gallery versions | Azure Marketplace (Ubuntu / RHEL / Debian, with provider-publisher whitelist) |
| GCP | Custom Images in your project | Public OS family catalogue (Debian, Ubuntu, Rocky, Windows Server) |

The deploy forms also accept a free-text "Deploy from AMI ID / URN /
Image URI" so you can launch from anything your account can see, not
only the curated lists.
