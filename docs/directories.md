# Directories

> **Audience:** operator · **Profile:** `demo` · **Read this when:** you want Windows servers joined to an Active Directory domain, want to manage an on-premises AD or LDAP directory, or want to browse and change group membership in Entra ID, Okta or PingOne.

**Preview.** Off by default. Turn it on with the **Directories** toggle under **Preview
features** in Settings (`directories_enabled`); its **Configure** link opens the settings
panel. The page is `/directories`, under **Directories** in the nav.

The one page holds two kinds of directory, which this section keeps apart:

- **Domains**, which a Windows server joins: Active Directory built in a cloud or already
  there, and your on-premises AD or LDAP reached through a remote agent.
- **Identity providers**, which a server never joins: Entra ID, Okta and PingOne, where
  users and groups live and where Entitle and Password Safe grant access.

| Page | Read it when |
|---|---|
| [Active Directory in the cloud](directories/active-directory.md) | you want Windows servers on AWS, GCP or Azure joined to a managed AD domain, built here or one that already exists |
| [On-premises directories](directories/on-premises.md) | you want to manage an on-prem AD or LDAP directory through a remote agent, extend your own domain to AWS or GCP, or have cloud servers Entra hybrid joined |
| [Identity providers](directories/identity-providers.md) | you want to browse users and groups in Entra ID, Okta or PingOne, or change group membership there |

Related: [OIDC and single sign-on](oidc.md) covers people **signing in** to the dashboard
through an identity provider, which is separate from managing one here.

The rest of this page applies to every kind of directory on the page.

## Linking a directory to its Entitle integration

**Entitle** on any directory's row lists the integrations in your Entitle tenant and lets
you **pin** the one that grants access to that directory. The row then shows it. The
integrations whose application looks like the directory's kind are listed first:

- Azure AD / Entra for Entra ID;
- Okta for Okta;
- PingOne for PingOne;
- Active Directory for AD.

That ordering is only a hint. Nothing is matched for you, and nothing is created or
changed in Entitle: the pin is a label on the dashboard's row. The integration id is checked
against Entitle's own list when you pin it, and the name shown is the one Entitle had then.

It reads `GET /public/v1/integrations` with the **Entitle** settings' API URL and token.
The URL must be your tenant's region (for example `api.us.entitle.io`); a wrong region
answers too, but with nobody's integrations. Pinning needs `directories:write` and is
audited as `directory_entitle_pin`.

## Settings

All on the **Directories** panel (Settings → Preview features → Directories → Configure):

| Key | Default | Meaning |
|---|---|---|
| `directories_enabled` | off | The preview toggle: page, nav and API. Demo profile only. |
| `directory_aws_default_edition` | `Standard` | Edition pre-selected on the AWS build form |
| `directory_gcp_reserved_ip_range` | — | Default /24 for GCP domain controllers |
| `directory_join_default_ou` | — | OU for joined servers when the deploy names none |
| `gcp_domain_join_service_account` | — | Service account GCE Windows servers run as when joining |
| `passwordsafe_directory_functional_account` | — | Password Safe functional account on an Active Directory platform |
| `passwordsafe_directory_change_password_on_register` | on | Rotate the administrator right after onboarding |
| `directory_idp_page_size` | `50` | Users or groups per page when browsing an identity provider (1–200) |

## Permissions

A `directories` scope (read / write / delete):

- **read:** list directories, browse an identity provider's users and groups, and test
  its connection.
- **write:** build, register, import, extend to AWS or GCP, read or reset the
  administrator password, toggle an identity provider's writes, and add or remove group
  members. A Config Management run against an on-prem directory needs it
  too, on top of `config_mgmt:write`.
- **delete:** destroy or unregister.

Importing from Password Safe, or picking a Password Safe account as an identity
provider's credential, also needs `secrets:use`.

It is checked explicitly, so a user with a custom permission map must be granted it. The
built-in read-only role reads it.

The **Join Active Directory** picker uses the deploying cloud's `write` permission
instead, because the person choosing a directory is the one deploying the server.
