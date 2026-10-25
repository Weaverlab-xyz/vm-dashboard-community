"""Serve the in-repo ``docs/*.md`` as rendered HTML at ``/docs/<page>``.

Backs the "guide" links in Settings (Action Guardrails, and the integration
guides) so an operator doesn't need the GitHub repo open. Public + read-only;
renders Markdown server-side (no CDN, works air-gapped).

``/docs`` is now wholly the documentation browser: an index at ``/docs`` plus the
rendered pages beneath it. The API explorer moved to ``/swagger`` (see main.py) —
previously FastAPI owned the exact ``/docs`` path, which made the two collide
confusingly.
"""
import html as _html
import posixpath as _posixpath
import re
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

# Everything GitHub's slugger deletes outright: anything that isn't a word
# character, a literal ASCII hyphen, or a space.
_SLUG_STRIP_RE = re.compile(r"[^\w\- ]", re.UNICODE)


class _GitHubSlugger:
    """Heading → anchor id, the way GitHub does it rather than the way
    python-markdown does it by default.

    The stock ``toc`` slugifier collapses a *run* of separators down to one
    hyphen, so ``## OCI (Autonomous Database) — read the caveats`` becomes
    ``#oci-autonomous-database-read-the-caveats`` here but
    ``#oci-autonomous-database--read-the-caveats`` on GitHub. Every anchor link
    in ``docs/`` is authored and checked against GitHub, so that one-character
    difference silently dumped the reader at the top of the page instead of at
    the section — 30 links' worth.

    GitHub (``github-slugger``) lowercases, *deletes* punctuation, turns spaces
    into hyphens, and de-duplicates with a ``-1``/``-2`` suffix. Deleting rather
    than replacing is the whole trick: `` — `` is space-nothing-space, which
    leaves two spaces and therefore two hyphens.

    Construct one per rendered page — the de-duplication counter is per-document,
    exactly like ``new GithubSlugger()`` is per file.

    Known, deliberate divergence: github-slugger keeps a bare variation selector
    (U+FE0F) when it strips the emoji in front of it, minting an anchor with an
    invisible character in it. We drop it. Headings are checked by
    ``tests/test_docs_anchors.py``, which will fail if a doc ever depends on one.
    """

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def __call__(self, value: str, separator: str) -> str:
        slug = _SLUG_STRIP_RE.sub("", value.lower()).replace(" ", separator)
        original = slug
        while slug in self._seen:
            self._seen[original] += 1
            slug = f"{original}{separator}{self._seen[original]}"
        self._seen[slug] = 0
        return slug


def _relative_link_extension(page: str):
    """Rewrite GitHub-relative ``*.md`` links into ``/docs/...`` routes.

    ``docs/`` is authored and reviewed on GitHub, so a cross-reference is written the way
    GitHub resolves it — ``[Cloud VMs](cloud-vms.md)``, or ``../../cloud-vms.md`` from a
    nested page. Nothing rewrote those, and there is no ``<base>`` in ``_SHELL``, so the
    browser resolved them against the current route: ``/docs/clouds/vms.md``, which makes
    ``doc_page`` look for ``docs/clouds/vms.md.md`` and 404. Every relative link in the tree
    was dead in this viewer while working perfectly on GitHub — the same split
    ``tests/test_app_docs_links.py`` was written about, one layer down.

    ``tests/test_docs_anchors.py`` resolves these links against the filesystem the way
    GitHub does, so it proves the *targets* exist; this makes the rendered href agree.

    Imports inside, so the module keeps its no-hard-dependency-on-markdown contract.
    """
    from markdown.extensions import Extension
    from markdown.treeprocessors import Treeprocessor

    base = _posixpath.dirname(page)

    class _Rewrite(Treeprocessor):
        def run(self, root):
            for a in root.iter("a"):
                href = a.get("href") or ""
                if href.startswith(("http://", "https://", "mailto:", "#", "/")):
                    continue
                path, _, frag = href.partition("#")
                if not path or not (path.endswith(".md") or path.endswith("/")):
                    continue
                target = _posixpath.normpath(_posixpath.join(base, path))
                # A ".." that climbs out of docs/ is a broken link either way; leave it
                # exactly as authored rather than inventing a route for it.
                if target.startswith(".."):
                    continue
                if target.endswith(".md"):
                    target = target[: -len(".md")]
                target = target.rstrip("/")
                # A directory link ("personas/") lands on that folder's README via the
                # fallback in doc_page.
                a.set("href", f"/docs/{target}" + (f"#{frag}" if frag else ""))

    class _Ext(Extension):
        def extendMarkdown(self, md):
            # After "toc" (which mints the heading ids) and after inline processing, so
            # every <a> exists by now. The priority is below toc's 5.
            md.treeprocessors.register(_Rewrite(md), "docs_relative_links", 4)

    return _Ext()


def _render_markdown(text: str, page: str | None = None) -> str:
    """Render Markdown → HTML. Imported lazily so a missing ``markdown`` lib can't
    crash app startup — the docs renderer is non-essential; it degrades to a
    readable <pre> fallback rather than taking the whole dashboard down.

    ``page`` is the doc's path under ``docs/`` without the ``.md`` suffix — i.e. exactly
    what ``doc_page`` was given. Passing it turns relative links into working routes (see
    ``_relative_link_extension``). It defaults to None so callers that only want the HTML,
    including the docs tests, need not care.
    """
    try:
        import markdown as _md
        from markdown.extensions.toc import TocExtension
        extensions = ["fenced_code", "tables", "sane_lists",
                      TocExtension(slugify=_GitHubSlugger())]
        if page is not None:
            extensions.append(_relative_link_extension(page))
        return _md.markdown(text, extensions=extensions)
    except ModuleNotFoundError:
        return f"<pre>{_html.escape(text)}</pre>"

router = APIRouter(tags=["docs"])

# repo_root/docs (parents[2] of this file is the app root; docs/ sits beside it,
# and is COPYed into the image — see Dockerfile).
_DOCS_DIR = (Path(__file__).resolve().parents[2] / "docs").resolve()

# Pages that moved, old route -> new route, both without ".md". A move repoints every link
# in the repo, but not a bookmark, a PRA jump-item note or a message somebody pasted
# months ago, and without this those are a bare 404 that reads as "the doc was deleted".
# doc_page consults this only after both real lookups miss, so an entry can never shadow a
# live page, and the target comes from this literal and never from the request, so it is
# no way round the traversal guard. Entries stay forever; tests/test_docs_moved_redirects
# pins that every target exists and that no source has come back to life.
#
# A key that names a FOLDER also covers every page under it -- see _moved_to. The browser
# carries a #fragment across the 301 itself, so deep links keep their section.
_MOVED = {
    # Hub folders, 2026-09 (docs/scheduling/, docs/integrations/beyondtrust/, ot-demo-cell/)
    "change-windows":                            "operations/scheduling/change-windows",
    "policy-guardrails":                         "operations/scheduling/policy-guardrails",
    "integrations/password-safe":                "beyondtrust/password-safe",
    "integrations/privileged-remote-access":     "beyondtrust/privileged-remote-access",
    "integrations/gateways":                     "beyondtrust/gateways",
    "integrations/entitle":                      "beyondtrust/entitle",
    "integrations/entitle-dashboard-permissions": "beyondtrust/entitle/dashboard-permissions",
    "integrations/epml":                         "beyondtrust/epml",
    "integrations/databases":                    "beyondtrust/password-safe/databases",
    "integrations/databases/password-safe":      "beyondtrust/password-safe/databases",
    "integrations/databases/password-safe-gcp":  "beyondtrust/password-safe/databases/gcp",
    "profiles/demo/ot-protocol-clients":         "profiles/demo/ot-demo-cell/ot-protocol-clients",
    # The Workload Lab folder, 2026-09. certificates.md was split three ways; its opening
    # section, the plugin reference, is what kept the name.
    "integrations/workload-cloud":               "preview/workload-lab/cloud",
    "integrations/workload-kubernetes":          "preview/workload-lab/kubernetes",
    "integrations/spiffe":                       "preview/workload-lab/spiffe",
    "integrations/workload-credentials":         "beyondtrust/workload-credentials",
    "integrations/certificates":                 "preview/workload-lab/certificates",
    # The OIDC section, 2026-10: SSO, Dex, the two Entra pages and the dashboard's own
    # identity (which had lived under remote-agents/ though it is about the clouds).
    "integrations/oidc":                         "platform/oidc",
    "integrations/entra-oauth":                  "platform/oidc/entra-oauth",
    "integrations/entra-k8s-federation":         "platform/oidc/k8s-federation/entra",
    # Kubernetes federation, 2026-10: one hub plus a page per IdP, Entra's among them.
    "oidc/entra-k8s-federation":                 "platform/oidc/k8s-federation/entra",
    "remote-agents/dashboard-identity":          "platform/oidc/dashboard-identity",
    # Remote agents under SPIFFE, 2026-10: an agent is a workload the dashboard does not
    # trust until it attests, so its pages live with the SPIRE server it attests to.
    "admin/remote-agents":                       "platform/spiffe/remote-agents",
    # Sections, 2026-10: the flat top level grouped into hub + folder sections the way
    # oidc/ is -- cloud/, access/, operations/, editions/, and kubesolo under kubernetes/.
    "cloud-vms":                                 "clouds/vms",
    "cloud-containers":                          "cloud-services/containers",
    "costs":                                     "operations/costs",
    "image-management":                          "operations/image-management",
    "infrastructure-as-code":                    "clouds/infrastructure-as-code",
    "CLOUD_SANDBOX":                             "clouds/sandbox",
    "virtual-desktops":                          "preview/virtual-desktops",
    "permissions":                               "admin/permissions",
    "service-accounts":                          "admin/service-accounts",
    "audit-log":                                 "admin/audit-log",
    "secrets-management":                        "admin/secrets-management",
    "job-worker":                                "operations/job-worker",
    "notifications":                             "admin/notifications",
    "auto-delete-timer":                         "operations/auto-delete-timer",
    "config-migration":                          "platform/config-migration",
    "cloud-hosting":                             "platform/cloud-hosting",
    "kubesolo":                                  "cloud-services/kubernetes/kubesolo",
    "saas-comparison":                           "editions/comparison",
    "saas-roadmap":                              "editions/roadmap",
    # Operations, 2026-10: managing what already runs and what the user built --
    # scheduling (with its folder), Config Management and storage join the section.
    "scheduling":                                "operations/scheduling",
    "config-management":                         "operations/config-management",
    "storage-management":                        "operations/storage-management",
    # Directories, 2026-10: out of cloud/ into its own section, split into cloud AD,
    # on-premises directories and identity providers. The old page lands on the hub.
    "cloud/directories":                         "preview/directories",
    # Password Safe for databases, 2026-10: out of beyondtrust/databases/ and into the
    # Password Safe section beside VM onboarding and token rotation, one page per cloud.
    # The old AWS + Azure page lands on the shared-model page, which links both.
    "integrations/beyondtrust/databases":        "beyondtrust/password-safe/databases",
    "integrations/beyondtrust/databases/password-safe":
        "beyondtrust/password-safe/databases",
    "integrations/beyondtrust/databases/password-safe-gcp":
        "beyondtrust/password-safe/databases/gcp",

    # Sections that mirror the app, 2026-10: one hub per nav group (Hypervisors, Clouds,
    # Cloud services, Preview, Operations, Admin), plus BeyondTrust and Platform from
    # Settings and Getting started. A folder that moved whole is one entry, which
    # carries every page under it.
    "directories":                               "preview/directories",
    "integrations/ansible":                      "platform/ansible",
    "integrations/beyondtrust":                  "beyondtrust",
    "integrations/portainer":                    "cloud-services/portainer",
    "integrations/rancher":                      "cloud-services/rancher",
    "kubernetes":                                "cloud-services/kubernetes",
    "oidc":                                      "platform/oidc",
    "onboarding":                                "getting-started",
    "operations/cloud-hosting":                  "platform/cloud-hosting",
    "remote-agents":                             "platform/spiffe/remote-agents",
    "workload-lab":                              "preview/workload-lab",
    # Workload Credentials is generally available and configured under Settings ->
    # BeyondTrust, so its guide went to that section rather than into preview/ with the lab.
    "workload-lab/workload-credentials":         "beyondtrust/workload-credentials",
    "ONBOARDING":                                "getting-started",
    "access":                                    "admin",
    "access/audit-log":                          "admin/audit-log",
    "access/permissions":                        "admin/permissions",
    "access/secrets-management":                 "admin/secrets-management",
    "access/service-accounts":                   "admin/service-accounts",
    "access/workload-identity":                  "platform/workload-identity",
    "cloud":                                     "clouds",
    "cloud/containers":                          "cloud-services/containers",
    "cloud/costs":                               "operations/costs",
    "cloud/image-management":                    "operations/image-management",
    "cloud/infrastructure-as-code":              "clouds/infrastructure-as-code",
    "cloud/sandbox":                             "clouds/sandbox",
    "cloud/virtual-desktops":                    "preview/virtual-desktops",
    "cloud/vms":                                 "clouds/vms",
    "cloud/windows-servers":                     "clouds/windows-servers",
    "databases":                                 "cloud-services/databases",
    "integrations":                              "README",
    "integrations/cloud-functions":              "cloud-services/functions",
    "integrations/hyperv":                       "hypervisors/hyperv",
    "integrations/mcp-server":                   "platform/mcp-server",
    "integrations/nutanix":                      "hypervisors/nutanix",
    "integrations/proxmox":                      "hypervisors/proxmox",
    "integrations/vmware":                       "hypervisors/vmware",
    "integrations/vsphere":                      "hypervisors/vsphere",
    "integrations/xcpng":                        "hypervisors/xcpng",
    "inventory":                                 "operations/inventory",
    "operations/config-migration":               "platform/config-migration",
    "operations/container-user":                 "platform/container-user",
    "operations/health":                         "admin/health",
    "operations/notifications":                  "admin/notifications",
    "spiffe":                                    "platform/spiffe",
    # Sub-pages into their hub's folder, 2026-10: beyondtrust/entitle-*.md under entitle/,
    # password-safe/databases-<cloud>.md under databases/.
    "beyondtrust/entitle-dashboard-permissions": "beyondtrust/entitle/dashboard-permissions",
    "beyondtrust/entitle-persona-bundles":       "beyondtrust/entitle/persona-bundles",
    "beyondtrust/entitle-virtual-apps":          "beyondtrust/entitle/virtual-apps",
    "beyondtrust/password-safe/databases-aws":   "beyondtrust/password-safe/databases/aws",
    "beyondtrust/password-safe/databases-azure": "beyondtrust/password-safe/databases/azure",
    "beyondtrust/password-safe/databases-gcp":   "beyondtrust/password-safe/databases/gcp",
}


def _moved_to(rel: str):
    """Where a moved page lives now, or None. An exact key wins; otherwise the longest key
    that is a folder prefix of ``rel`` carries the rest of the path with it, so
    a page under a moved folder follows its folder. Only into a folder: a key whose target
    is a page (``integrations`` -> the docs index) carries nothing, so an unknown page under
    it is a 404 rather than a redirect to ``README/<page>``."""
    if rel in _MOVED:
        return _MOVED[rel]
    for old in sorted(_MOVED, key=len, reverse=True):
        if rel.startswith(old + "/") and (_DOCS_DIR / _MOVED[old]).is_dir():
            return _MOVED[old] + rel[len(old):]
    return None

# Only these sections are surfaced on the /docs index. Other subdirectories
# (design/, notes/, runbooks/, profiles/pov/design/) still render if you know the path —
# they're just internal enough that we don't want them cluttering the operator-facing index.
# "General" is the synthetic name for docs that live at the docs/ root.
#
# A section is the doc's parent directory as a relative path, so a nested folder joins this
# set under its full path ("profiles/demo/personas"), not its basename. That is also why
# every member needs a _SECTION_LABELS entry: a path reads as a path in an <h2>.
#
# `personas` is operator-facing: one page per role explaining which demo to run and why, so
# it belongs in the index rather than behind a known path. NOTE that adding it does NOT make
# this module persona-aware — the index still lists every file in the directory in the same
# order for everybody. This shell is public and unauthenticated (see `_shell`), so reordering
# it by the instance's chosen focus would leak that focus to anyone who asks; the
# persona-aware view of the same material is /use-cases, behind the auth shell.
#
# The same reasoning is why BOTH profiles' sections are listed to everybody rather than
# filtered by install_profile(): making index *content* vary with instance state is the leak
# above wearing a different hat. A demo instance already lists the POV pages today.
_INDEX_SECTIONS = {
    "General",
    "getting-started",
    "hypervisors",
    "clouds",
    "clouds/vms",
    "clouds/sandbox",
    "cloud-services",
    "cloud-services/databases",
    "cloud-services/functions",
    "cloud-services/kubernetes",
    "cloud-services/portainer",
    "cloud-services/rancher",
    "preview",
    "preview/directories",
    "preview/workload-lab",
    "preview/workload-lab/spiffe",
    "preview/workload-lab/certificates",
    "preview/workload-lab/certificate-lab",
    "operations",
    "operations/config-management",
    "operations/image-management",
    "operations/storage-management",
    "operations/job-worker",
    "operations/scheduling",
    "admin",
    "admin/secrets-management",
    "admin/permissions",
    "beyondtrust",
    "beyondtrust/gateways",
    "beyondtrust/password-safe",
    "beyondtrust/password-safe/databases",
    "beyondtrust/entitle",
    "platform",
    "platform/oidc",
    "platform/oidc/k8s-federation",
    "platform/oidc/k8s-federation/entra",
    "platform/spiffe",
    "platform/spiffe/remote-agents",
    "platform/spiffe/remote-agents/hypervisors",
    "platform/cloud-hosting",
    "platform/cloud-hosting/azure-container-apps",
    "platform/ansible",
    "editions",
    "profiles",
    "profiles/demo",
    "profiles/demo/personas",
    "profiles/demo/ot-demo-cell",
    "profiles/demo/agent-demo-cell",
    "profiles/pov",
    "profiles/pov/public-cloud",
    "profiles/pov/skytap",
    "profiles/pov/customer-access",
}


# Heading text per section, in the order the /docs index lists them: the app's own order.
# Without this a nested section renders as <h2>profiles/demo/personas</h2> — a directory
# path where the index wants a phrase.
_SECTION_LABELS = {
    "General":                      "Platform reference",
    "getting-started":              "Getting started · per-cloud setup",
    "hypervisors":                  "Hypervisors",
    "clouds":                       "Clouds",
    "clouds/vms":                   "Clouds · VMs",
    "clouds/sandbox":               "Clouds · Cloud Sandbox",
    "cloud-services":               "Cloud services",
    "cloud-services/databases":     "Cloud services · Databases",
    "cloud-services/functions":     "Cloud services · Cloud Functions",
    "cloud-services/kubernetes":    "Cloud services · Kubernetes",
    "cloud-services/portainer":     "Cloud services · Portainer",
    "cloud-services/rancher":       "Cloud services · Rancher",
    "preview":                      "Preview",
    "preview/directories":          "Preview · Directories",
    "preview/workload-lab":         "Preview · Workload Lab",
    "preview/workload-lab/spiffe":  "Preview · Workload Lab · SPIFFE",
    "preview/workload-lab/certificates":"Preview · Workload Lab · certificates",
    "preview/workload-lab/certificate-lab":"Preview · Workload Lab · certificate lab",
    "operations":                   "Operations",
    "operations/config-management": "Operations · Config Management",
    "operations/image-management":  "Operations · Image Management",
    "operations/storage-management":"Operations · Storage Management",
    "operations/job-worker":        "Operations · Job Worker",
    "operations/scheduling":        "Operations · scheduling",
    "admin":                        "Admin",
    "admin/secrets-management":     "Admin · Secrets Management",
    "admin/permissions":            "Admin · Permissions",
    "beyondtrust":                  "BeyondTrust",
    "beyondtrust/gateways":         "BeyondTrust · Gateway hosts",
    "beyondtrust/password-safe":    "BeyondTrust · Password Safe",
    "beyondtrust/password-safe/databases":"BeyondTrust · Password Safe · databases",
    "beyondtrust/entitle":          "BeyondTrust · Entitle",
    "platform":                     "Platform",
    "platform/oidc":                "Platform · OIDC and single sign-on",
    "platform/oidc/k8s-federation": "Platform · OIDC · Kubernetes federation",
    "platform/oidc/k8s-federation/entra":"Platform · OIDC · Kubernetes federation · Entra",
    "platform/spiffe":              "Platform · SPIFFE and SPIRE",
    "platform/spiffe/remote-agents":"Platform · SPIFFE · remote agents",
    "platform/spiffe/remote-agents/hypervisors":"Platform · SPIFFE · remote agents · hypervisors",
    "platform/cloud-hosting":       "Platform · cloud hosting",
    "platform/cloud-hosting/azure-container-apps":"Platform · cloud hosting · Azure Container Apps",
    "platform/ansible":             "Platform · Remote Worker runners",
    "editions":                     "Editions",
    "profiles":                     "Demo and POV profiles",
    "profiles/demo":                "Demo profile",
    "profiles/demo/personas":       "Demo profile · personas",
    "profiles/demo/ot-demo-cell":   "OT Demo Cell",
    "profiles/demo/agent-demo-cell":"Agent Demo Cell",
    "profiles/pov":                 "POV profile",
    "profiles/pov/public-cloud":    "POV profile · public cloud",
    "profiles/pov/skytap":          "POV profile · Skytap",
    "profiles/pov/customer-access": "POV profile · customer access",
}


# Titles for docs whose filename is an identifier rather than a phrase. The persona pages are
# named after the persona key so /docs/profiles/demo/personas/<key> matches the
# vocabulary the app, the API and the nav all use -- which is right for the URL and
# useless as a label: the index derives its text from the filename, so these would
# list as "Ot", "Dba" and "Sre".
#
# An explicit map, deliberately NOT "use each doc's H1 for every page". Measured across the 53
# indexed docs that would change 40 titles and several for the worse -- one H1 is a
# 78-character sentence, and an index entry is a link rather than a headline.
#
# These must match services/personas.Persona.label; tests/test_persona_docs pins that, so the
# two cannot drift. The map is a literal rather than an import because this module is
# deliberately independent of the persona registry -- see the note on _INDEX_SECTIONS above,
# and _shell below for why this shell must never learn the ACTIVE persona.
_TITLE_OVERRIDES = {
    "profiles/demo/personas/cloudops":   "Cloud Ops engineer",
    "profiles/demo/personas/devops":     "DevOps engineer",
    "profiles/demo/personas/hypervisor": "Hypervisor admin",
    "profiles/demo/personas/itops":      "IT engineer",
    "profiles/demo/personas/ot":         "OT / ICS engineer",
    "profiles/demo/personas/dba":        "DBA / data platform",
    "profiles/demo/personas/security":   "Security analyst",
    "profiles/demo/personas/sre":        "Platform / SRE",
    "profiles/demo/personas/netadmin":   "Network / firewall admin",
    "profiles/demo/personas/finops":     "FinOps / cloud governance",
    "profiles/demo/personas/aiops":      "AI / agent platform",
    "profiles/demo/personas/iamops":     "IAM engineer",
    "profiles/demo/personas/idarch":     "Identity architect",
    # Not personas. The derived title is the filename title-cased, which reads as a
    # filename ("Ps Runbook", "Standing One Up") rather than as the page.
    "cloud-services/kubernetes/kubesolo": "KubeSolo",
    "cloud-services/kubernetes/k8s": "Managed Kubernetes (k8s)",
    "cloud-services/kubernetes/k3s": "k3s",
    "profiles/demo/ot-demo-cell":        "OT Demo Cell",
    "profiles/demo/net-demo-cell":       "Network Demo Cell",
    "profiles/demo/agent-demo-cell":     "Agent Demo Cell",
    "profiles/pov/standing-one-up":      "Standing up a POV instance",
    "profiles/pov/public-cloud":         "Running POVs on a public cloud",
    "profiles/pov/gateway-and-broker":   "The Gateway and the Resource Broker",
    "profiles/pov/wiring":               "Wiring VMs into PRA, Password Safe and Entitle",
    "profiles/pov/demo-cells":           "Demo cells in a POV",
    "profiles/pov/customer-access":      "What the customer sees",
    "profiles/pov/lifecycle":            "Keeping a POV true, and reaping it",
    "profiles/pov/ps-runbook":           "Password Safe POC runbook",
    "beyondtrust/password-safe/databases":       "Databases: the shared model",
    "beyondtrust/password-safe/databases/aws":   "Databases on AWS (dbssm)",
    "beyondtrust/password-safe/databases/azure": "Databases on Azure (dbazure)",
    "beyondtrust/password-safe/databases/gcp":   "Databases on GCP Cloud SQL (dbgcp)",
    "platform/cloud-hosting/azure-container-apps": "Azure Container Apps",
    "platform/cloud-hosting/gcp-cloud-run":        "GCP Cloud Run",
    "platform/cloud-hosting/aws-ecs":              "AWS ECS Fargate",
    "profiles/demo/ot-demo-cell/identity":           "Who brokers identity",
    "profiles/demo/ot-demo-cell/deploying":          "Deploying a cell",
    "profiles/demo/ot-demo-cell/verification":       "End-to-end verification",
    "getting-started/aws":                    "AWS setup",
    "getting-started/azure":                  "Azure setup",
    "getting-started/gcp":                    "GCP setup",
    "getting-started/after-first-run":        "After the first run",
    "getting-started/feature-test":           "Feature-test checklist",
    "getting-started/troubleshooting":        "Onboarding troubleshooting",
    # The OIDC section. "Oidc" and "Entra K8s Federation" read as filenames.
    "platform/oidc":                              "OIDC and single sign-on",
    "platform/oidc/entra-oauth":                  "Sign in with Microsoft (Entra)",
    "platform/oidc/k8s-federation":               "Kubernetes federation",
    "platform/oidc/k8s-federation/entra":         "Kubernetes federation: Entra ID",
    "platform/oidc/k8s-federation/okta":          "Kubernetes federation: Okta",
    "platform/oidc/k8s-federation/pingone":       "Kubernetes federation: PingOne",
    "platform/oidc/k8s-federation/oidc":          "Kubernetes federation: other OIDC",
    "platform/oidc/dashboard-identity":           "The dashboard's own identity",
    "platform/spiffe":                            "SPIFFE and SPIRE",
    "platform/oidc/dex":                          "Dex",
    # The section hubs and their pages. Folder names were dropped from the filenames
    # (cloud-vms -> cloud/vms), so the derived titles lose the word that said what they were.
    "admin":                            "Identity and access",
    "editions":                          "Editions",
    "clouds/vms":                         "Cloud VMs",
    "cloud-services/containers":                  "Cloud Containers",
    "operations/costs":                       "Cloud Costs",
    "clouds/sandbox":                     "Cloud Sandbox",
    "clouds/infrastructure-as-code":      "Infrastructure as Code",
    "operations/auto-delete-timer":      "Auto-delete Timer",
    "editions/comparison":               "Community vs. hosted",
    "editions/roadmap":                  "SaaS Roadmap",
    # The BeyondTrust folder. "Epml" is not a word, and "Entitle Dashboard Permissions"
    # reads as a settings page rather than the Entitle mechanism it describes.
    "beyondtrust/epml":     "EPM for Linux",
    "beyondtrust/entitle/dashboard-permissions": "Entitle dashboard permissions",
    "beyondtrust/gateways": "Gateway hosts",
    "profiles/demo/ot-demo-cell/ot-protocol-clients": "OT protocol clients on Windows",
    # The filename predates the product name; Settings and the page's own H1 say this.
    "operations/scheduling/policy-guardrails": "Action Guardrails",
    # The Workload Lab folder. Derived titles read as filenames here -- "Spiffe", "Cloud",
    # "Kubernetes", "Subordinate Ca" -- and the last of those is simply wrong. "Cloud" and
    # "Kubernetes" are also the short names of two unrelated top-level pages, so without
    # these the index lists the same word twice under different sections.
    "preview/workload-lab/spiffe":               "SPIFFE and SPIRE",
    "preview/workload-lab/cloud":                "Short-lived cloud credentials",
    "preview/workload-lab/kubernetes":           "Workload access to Kubernetes",
    "preview/workload-lab/certificate-lab":      "The Certificate Lab",
    "preview/workload-lab/subordinate-ca":       "Onboarding a subordinate CA",
    "beyondtrust/workload-credentials": "BeyondTrust Workload Credentials",
    "preview/workload-lab/dynamic-credentials":  "Dynamic AWS and Azure credentials",
    "preview/workload-lab/consumers":            "What consumes these credentials",
    "platform/ansible/config-reference":  "Config field reference",
    "platform/ansible/shared-cloud":      "Shared cloud infrastructure",
    "platform/ansible/ansible-runner":    "The Ansible runner",
    "platform/ansible/kubernetes-runner": "The Kubernetes runner",
    "platform/ansible/secrets":           "Secrets in a run",
    "platform/ansible/playbooks":         "Playbook structure",
    "platform/spiffe/remote-agents/enrolment":               "Enrolling an agent",
    "platform/spiffe/remote-agents/agent-host":              "Running the agent on your host",
    "platform/spiffe/remote-agents/hypervisors":             "Hypervisor connections",
    "platform/spiffe/remote-agents/credentials":             "Credentials an agent uses",
    "platform/spiffe/remote-agents/config-runs":             "Agent-executed Config Management",
    "platform/spiffe/remote-agents/file-shares":             "Agent-brokered file shares",
}

_SHELL = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · {brand} Docs</title>
<link rel="icon" href="{favicon}">
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin:0; background:{body_bg}; color:#0f172a;
         font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  header {{ background:{nav_bg}; color:{nav_fg}; }}
  header .lockup {{ max-width:820px; margin:0 auto; padding:.85rem 1.25rem;
                    display:flex; align-items:center; gap:.6rem; }}
  header .brand {{ font-size:1.05rem; font-weight:700; letter-spacing:-.01em; }}
  header .slash {{ color:{nav_weft}; font-weight:300; }}
  header .product {{ font-size:.85rem; opacity:.9; }}
  .rail {{ height:4px; background:{rail}; }}
  header img.logo {{ height:26px; width:auto; max-width:200px; display:block; }}
  main {{ max-width:820px; margin:0 auto; padding:2.5rem 1.25rem 4rem; }}
  a {{ color:{link}; }}
  h1,h2,h3 {{ line-height:1.25; margin-top:2rem; }}
  h1 {{ font-size:1.9rem; }} h2 {{ font-size:1.4rem; }} h3 {{ font-size:1.15rem; }}
  code {{ background:#eef2f7; padding:.1em .35em; border-radius:4px; font-size:.9em; }}
  pre {{ background:#0f172a; color:#e2e8f0; padding:1rem; border-radius:8px; overflow:auto; }}
  pre code {{ background:none; padding:0; color:inherit; }}
  table {{ border-collapse:collapse; width:100%; margin:1rem 0; }}
  th,td {{ border:1px solid #e2e8f0; padding:.5rem .7rem; text-align:left; vertical-align:top; }}
  th {{ background:#f1f5f9; }}
  blockquote {{ border-left:3px solid #cbd5e1; margin:1rem 0; padding:.25rem 1rem; color:#475569; }}
  .back {{ font-size:.85rem; }}
</style></head>
<body>
<header><div class="lockup">
{mark}
<span class="brand">{brand}</span><span class="slash">/</span><span class="product">{product}</span>
</div></header>{rail_el}
<main>
<p class="back"><a href="/settings">← Back to dashboard</a></p>
{body}
</main></body></html>"""


def _shell(title: str, body: str) -> str:
    """Wrap rendered Markdown in the branded shell.

    One helper rather than two direct ``.format()`` call sites: the shell takes a dozen
    fields instead of two, and threading those through both callers by hand is how one of
    them ends up a version behind.

    The profile lookup is best-effort on purpose. This module's whole premise is that the
    docs renderer is non-essential and degrades to a readable ``<pre>`` fallback rather
    than taking the whole dashboard down -- ``install_profile()`` reads ``config_service``,
    so a database that is down or not yet migrated must yield an unbranded-but-readable
    page, never a 500. The env default is the same fallback ``feature_flags`` itself uses.

    An uploaded logo renders here too, which means it is served to anonymous visitors. That
    is already true of the brand name below and of the sign-in page, so it is not a new
    exposure -- but it is a decision: an operator who uploads an internal-only mark is
    publishing it to anyone who can reach ``/docs``.

    Note ``/docs`` is public and unauthenticated, so this makes the profile name readable
    without signing in. That is deliberate, and already true of ``GET /api/features`` which
    serves ``install_profile`` to anyone -- but it is a decision, not an accident, and it
    would need revisiting if that endpoint were ever locked down.

    ``brand`` and ``product`` are escaped because ``_SHELL`` is a ``.format()`` template,
    not a Jinja one: nothing here autoescapes. That was safe while the brand was a constant
    in ``ui_theme``; it stopped being safe the moment an admin could set it in Settings,
    since this page is the one place an operator-supplied string renders to an anonymous
    visitor. ``services.branding`` strips control characters and caps the length, but that
    is defence in depth -- this escape is the control.
    """
    from ..config import settings
    from ..services import branding, ui_theme
    try:
        from ..services import feature_flags
        profile = feature_flags.install_profile()
    except Exception:
        profile = settings.install_profile
    theme = ui_theme.theme_for(profile, settings.app_env, **branding.overrides())
    rail = theme["hex"]["rail"]

    # The third copy of the brand mark, and the one that cannot be a template conditional:
    # _SHELL is a .format() string, not Jinja. Built here rather than as a second _SHELL
    # variant for the reason this function exists at all (see the docstring above) -- two
    # shells is how one of them ends up a version behind.
    #
    # This is HTML we construct, so unlike `brand` and `product` it must NOT be escaped.
    # The URL is safe because it is built from a digest that services.branding matched
    # against ^[0-9a-f]{64}$ -- never from a raw config read. Keep it that way: _SHELL does
    # not autoescape, and this page is public.
    logo = theme.get("logo")
    if logo:
        mark = f'<img class="logo" src="{logo["url"]}" alt="" aria-hidden="true">'
    else:
        mark = (
            '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke-width="2.2"'
            ' stroke-linecap="round" aria-hidden="true">'
            f'<path d="{theme["mark_warp_path"]}" stroke="{theme["hex"]["nav_warp"]}"/>'
            f'<path d="{theme["mark_weft_path"]}" stroke="{theme["hex"]["nav_weft"]}"/></svg>'
        )

    return _SHELL.format(
        mark=mark,
        title=title,
        body=body,
        brand=_html.escape(theme["brand"]),
        product=_html.escape(theme["product"]),
        favicon=theme["favicon"],
        rail=rail or "transparent",
        rail_el='<div class="rail"></div>' if rail else "",
        **{k: theme["hex"][k]
           for k in ("nav_bg", "nav_fg", "nav_warp", "nav_weft", "body_bg", "link")},
    )


@router.get("/docs", response_class=HTMLResponse)
async def doc_index() -> HTMLResponse:
    """Index of every shipped doc, grouped by directory.

    Without this, ``/docs`` 404s and the guides are only reachable if you already
    know the exact path — which was the whole discoverability problem.
    """
    if not _DOCS_DIR.is_dir():
        raise HTTPException(status_code=404, detail="docs directory not found")

    groups: dict = {}
    indexes: dict = {}
    for path in sorted(_DOCS_DIR.rglob("*.md")):
        rel = path.relative_to(_DOCS_DIR).with_suffix("")
        section = str(rel.parent).replace("\\", "/")
        section = "General" if section == "." else section
        if section not in _INDEX_SECTIONS:
            continue
        href = str(rel).replace("\\", "/")
        if rel.name == "README":
            # A folder's own index page. It becomes that section's HEADING rather than an
            # entry under it: nine list items all titled "Readme" is not an index, and
            # docs/README.md would list as a link to a second copy of this page.
            # The FOLDER url, not ".../README" -- doc_page falls back to the README, and a
            # path with mixed case in it is the kind of link that works on a Windows
            # checkout and 404s in the container. "General" is synthetic rather than a
            # directory, so the root index is the one that needs its real href.
            indexes[section] = href if section == "General" else section
            # Registers the section even when the README is the only .md in it, which is the
            # case for docs/profiles/ -- otherwise that folder's index is unreachable from
            # here. Also keeps the loop below, ordered by _SECTION_LABELS, the one place
            # sections are ordered.
            groups.setdefault(section, [])
            continue
        title = (_TITLE_OVERRIDES.get(href)
                 or rel.name.replace("-", " ").replace("_", " ").title())
        groups.setdefault(section, []).append((title, href))

    # A folder split out of a page -- onboarding/, remote-agents/, oidc/, cloud/,
    # integrations/ansible/ and the rest -- has no README of its own, because doc_page resolves
    # <page>.md before <page>/README.md and the hub has to win that URL. Its index is
    # that sibling hub, so hang the heading off it rather than leaving the section
    # unlinked.
    for section in groups:
        if section in indexes or section == "General":
            continue
        # Matched by LISTING, not by is_file(): ONBOARDING.md indexes onboarding/, and
        # (_DOCS_DIR / "onboarding.md").is_file() is True on an NTFS dev checkout and
        # False in the Linux image -- so a case-insensitive match here would link the
        # heading locally and silently drop it in production.
        parent = _DOCS_DIR / PurePosixPath(section).parent
        want = f"{PurePosixPath(section).name}.md".lower()
        hub = next((p for p in parent.iterdir()
                    if p.name.lower() == want and p.is_file()), None)
        if hub is not None:
            indexes[section] = str(
                hub.relative_to(_DOCS_DIR).with_suffix("")).replace("\\", "/")

    parts = ["<h1>Documentation</h1>",
             '<p>Shipped with this build. The API explorer lives at '
             '<a href="/swagger">/swagger</a>.</p>']
    # In _SECTION_LABELS's order, which is the app's: its menu groups, then the Settings
    # groups, then the profiles. Every indexed section has a label (test_docs_conventions).
    order = list(_SECTION_LABELS)
    for section in sorted(groups, key=lambda s: (order.index(s) if s in order else len(order), s)):
        label = _html.escape(_SECTION_LABELS.get(section, section))
        index_href = indexes.get(section)
        if index_href:
            label = f'<a href="/docs/{_html.escape(index_href)}">{label}</a>'
        parts.append(f"<h2>{label}</h2>")
        if groups[section]:
            parts.append("<ul>")
            for title, href in groups[section]:
                parts.append(
                    f'<li><a href="/docs/{_html.escape(href)}">{_html.escape(title)}</a></li>')
            parts.append("</ul>")
    return HTMLResponse(_shell("Documentation", "".join(parts)))


@router.get("/docs/{page:path}", response_class=HTMLResponse)
async def doc_page(page: str) -> HTMLResponse:
    """Render ``docs/<page>.md``, or ``docs/<page>/README.md`` for a folder. 404 if it's
    missing or escapes the docs dir."""
    rel = page.strip("/")
    if not rel:
        raise HTTPException(status_code=404, detail="doc not found")
    candidate = (_DOCS_DIR / f"{rel}.md").resolve()
    # Path-traversal guard: the resolved file must live under docs/.
    try:
        candidate.relative_to(_DOCS_DIR)
    except ValueError:
        raise HTTPException(status_code=404, detail="doc not found")
    # The link rewriter resolves relative hrefs against dirname(page), so `page` has to be
    # the FILE's path and not the URL. They differ on exactly one route -- the folder URL
    # below -- and there the difference silently broke every relative link on every hub
    # page in the tree: /docs/profiles/pov resolved `skytap.md` against `profiles/`
    # instead of `profiles/pov/`, sending the reader to /docs/profiles/skytap and a 404.
    source = rel
    if not candidate.is_file():
        # A folder URL serves that folder's index, so /docs/profiles/pov works and every
        # link can name the directory the way it does on GitHub. Without this the hub is
        # reachable only at /docs/profiles/pov/README -- mixed case, on a case-sensitive
        # container filesystem, which is a link that passes a check on Windows and 404s in
        # the image.
        candidate = (_DOCS_DIR / rel / "README.md").resolve()
        try:
            candidate.relative_to(_DOCS_DIR)
        except ValueError:
            raise HTTPException(status_code=404, detail="doc not found")
        if not candidate.is_file():
            moved = _moved_to(rel)
            if moved is not None:
                return RedirectResponse(f"/docs/{moved}", status_code=301)
            raise HTTPException(status_code=404, detail="doc not found")
        source = f"{rel}/README"

    html = _render_markdown(candidate.read_text(encoding="utf-8"), page=source)
    # Escape the page-derived title before reflecting it into the HTML shell —
    # it originates from the request path, so render it as text, not markup
    # (prevents reflected XSS; CodeQL py/reflective-xss).
    title = _html.escape(rel.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").title())
    return HTMLResponse(_shell(title, html))
