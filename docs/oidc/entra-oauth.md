# Sign in with Microsoft (Entra OAuth)

> **Audience:** operator · **Profile:** `both` · **Read this when:** you already use the legacy Entra-only sign-in button, or are moving from it to generic OIDC.

The older, Entra-specific sign-in path. **It is legacy:** generic
[OIDC single sign-on](../oidc.md) is the default, works with Entra ID, and is what the setup
wizard's Admin step now offers. This path still works and can run alongside it, but new
setups should use OIDC.

### Moving from this button to OIDC

You can reuse the app registration you already have:

1. In the app registration, add a second redirect URI:
   `{your-host}/api/auth/oauth/oidc/callback`. Keep the old one until you have switched.
2. Make sure the ID token carries a **groups** claim (**Token configuration → Add groups
   claim**), if you map groups to workgroups.
3. In **Settings → Integrations → Single sign-on (OIDC)**, set the issuer to
   `https://login.microsoftonline.com/<tenant-id>/v2.0`, with the same client ID and secret.
   Use **Test** to check discovery.
4. Sign in with the new button. Then clear the **Sign in with Microsoft (legacy)** fields
   (setup wizard → Azure step) to remove the old button.

People keep their accounts: both paths read the email from the same claims, and OIDC falls
back to the email when it does not recognise the subject. Group mappings carry over too,
because Entra sends group **object IDs** in both cases.


Optional. Lets users log in with their work Microsoft account instead of
a local password.

### Create a second Azure app registration

This is a **different** registration from the resource-management service
principal in Part B.

1. Azure Portal → **App registrations** → **New registration**.
   - Name: `Dashboard OAuth (dev)`
   - Supported account types: single-tenant
2. **Authentication** → **Add platform** → **Web**.
   - Redirect URI: `http://localhost:8001/api/auth/oauth/azure/callback`
3. **API permissions** → **Add a permission** → **Microsoft Graph** →
   **Delegated** → `openid`, `profile`, `email`.
4. **Certificates & secrets** → **New client secret**. Copy the value.

### Wire it up

**During initial setup:** In the setup wizard, go to the Azure step and
expand the **Sign in with Microsoft (legacy) — optional** panel. Enter the Client
ID, Client Secret, and Tenant ID, then complete the wizard as normal.

**After initial setup:** Navigate to `/setup` in your browser (admin
login required). The wizard reopens in reconfigure mode. Go to Step 3
and expand the OAuth panel — the Client ID and Tenant ID will be
pre-filled if already configured; leave the secret field blank to keep
the stored value.

The redirect URI is derived automatically from your browser's host —
you do not set it in the dashboard. Register the same URI that appears
in the wizard hint (`{your-host}/api/auth/oauth/azure/callback`) in the
Azure app registration under **Authentication**.

Once saved, the login page shows a **Sign in with Microsoft** button
without a restart. Until a client ID and tenant are saved there is no button at all, and
when [generic OIDC](../oidc.md) is also configured its button comes first.

Optional: map Entra group object IDs to dashboard workgroups from
**Settings → Groups** — users in a mapped group are auto-created and
assigned workgroups on first OAuth login.

---
