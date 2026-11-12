# Feedback

> **Audience:** operator · **Profile:** `both` · **Read this when:** you want the people using this dashboard — including the prospect in an evaluation — to be able to tell you what is wrong without leaving the page.

Part of [Admin](../admin.md).

Feedback about this dashboard used to arrive out of band: somebody said it in a demo, or
mentioned it in a chat, or opened a GitHub issue by hand after reading `CONTRIBUTING.md`.
All three lose the part that makes a report actionable — which page, which build, which
instance — and most of them never reach the tracker at all.

`feedback_enabled` puts a **Send feedback** button in the menu on every page, and
optionally on the POV access page an evaluation guest sees. Each submission is recorded in
this dashboard and, depending on configuration, filed as a GitHub issue.

Off by default, and **with no destination by default** — see
[Choosing the repository](#choosing-the-repository), which is the decision that matters.

---

## The two modes

The mode is **derived from configuration, not chosen**. That is deliberate: there is no
way to be in a state where the panel says one thing and the button does another.

| Repository | Token | Mode | What happens on submit |
|---|---|---|---|
| set | set | **Direct** | The dashboard files the issue itself and shows the submitter the issue number straight away. |
| set | blank | **Compose** | The dashboard opens a GitHub issue form with the title and body already filled in. The submitter presses Submit and files it under **their own** GitHub account. |
| blank | — | **Off** | The button is hidden, whatever the toggle says. |

**Compose mode is a supported configuration, not a half-finished one.** It is the right
shape for a self-hosted copy of this dashboard: the submitter still gets every
auto-captured field, no credential is stored anywhere on the instance, and GitHub's own
abuse controls do the rate limiting. If you run this dashboard yourself and want to report
something upstream, set the repository to `Weaverlab-xyz/vm-dashboard-community`, leave the
token blank, and the button will work.

An **evaluation guest never gets the compose link**, even in compose mode. A prospect is
not a contributor to the repository and may have no GitHub account at all, so handing them
an issue form to file themselves is a dead end dressed up as success. Their submission is
recorded here instead, and you read it on the all-submissions view — which is what their
form means when it says this reached the team.

Compose mode still records the submission locally, with the **full** text, even though the
link itself carries a shortened copy — a GitHub issue URL is a GET, and a pasted traceback
will otherwise push it past what a browser or proxy will carry.

## Choosing the repository

`feedback_github_repo` ships **blank**, not pointing at anything. That is the one piece of
deliberate friction in this feature.

> **A GitHub issue in a public repository is readable by anyone, forever.** A prospect
> filling in this form will quite reasonably name their own company, their own hostnames,
> or what they were trying to do with your product. If evaluation guests can submit, point
> this at a **private** repository.

With a default, an operator who pasted in a token without changing the repository would
publish a prospect's wording to the world and never be asked about it. So the destination
has to be named.

The form tells the submitter where their words are going: it reads the repository's
visibility from GitHub and says, above the box, whether the issue will be public, private,
or — if that could not be checked — that they should assume public.

## Setting it up

Settings → Integrations → **In-product feedback**.

1. **Repository** — `owner/name`. Private, if guests can submit.
2. **GitHub token** — a fine-grained PAT with `Issues: read and write` on that one
   repository and nothing else. Leave blank for compose mode. Stored encrypted and never
   returned by the API.
3. **Labels** — comma separated, applied to every issue. **The labels must already exist
   in the repository**; GitHub rejects an issue carrying a label that does not, and the
   submission will come back as "not filed".
4. **Let evaluation guests submit** — adds the button to the POV access page.
5. **Caps** — see [Rate limiting](#rate-limiting).
6. Turn the toggle on.

## What is captured

| Field | Where it comes from |
|---|---|
| Page | The browser, the only field it supplies |
| Version, commit | The server, from its own build provenance |
| Instance profile | The server |
| Submitted by | The authenticated principal |
| POV | For a guest, the evaluation they were in |

Everything except the page is read **server-side on purpose**. A client that could set its
own version could pin a bug on a build that never had it.

No email address is ever included.

## Credentials are refused, not stripped

Title and body are scanned for things that look like credentials — AWS keys, GitHub
tokens, private keys, `password:`-style assignments. On a match the submission is
**refused with an explanation naming what matched**, and nothing is filed or stored.

Refusing rather than quietly removing is the point. A submission silently emptied of its
content and filed anyway is a change the submitter never asked for and cannot see; a
refusal arrives while their text is still on screen, so they can fix it themselves.

Treat the scan as a safety net rather than a guarantee: it does not recognise Azure client
secrets, bearer tokens or connection strings. The repository's visibility, stated in the
form, is the control that does not depend on pattern matching.

## Rate limiting

A sliding window counted off the submissions themselves — there is no separate counter, so
it behaves the same across however many workers the dashboard runs.

- **Per person** (default 5) and **instance-wide** (default 50), over a **window** in hours
  (default 24).
- `0` removes a cap.
- Over the cap, a submitter gets a 429 with `Retry-After`. It is **not a lockout**: it
  lifts on its own, because anything that could lock a real user out of reporting a bug is
  worse than the flood it prevents.

## Who sees what

| Surface | Who |
|---|---|
| The button and form | Anyone signed in, plus evaluation guests if enabled |
| `/feedback` — what I have sent | Everyone, filtered to their own submissions |
| All submissions | Administrators only |

There is **no grantable permission scope** for feedback, deliberately. Adding one would
silently revoke it from every role that exists today, which is a steep price for a feature
whose whole point is that nobody is excluded from it. Submissions are filtered by row
instead; the all-submissions table is admin-only because those rows hold whatever somebody
typed, a prospect included.

## Issue state

`feedback_poll_minutes` ships at `0`: the dashboard records the issue link and does not ask
GitHub about it again. `/feedback` links out, and GitHub is the source of truth for whether
something was fixed.

Raising it is for instances that want open/closed shown in-app. Note that it polls the
issue **list** once per pass rather than each issue in turn — one request per pass keeps
this inside the API budget, where a request per row would not.

## When something does not work

| Symptom | Cause |
|---|---|
| No button anywhere | `feedback_enabled` off, or no repository set |
| "Feedback has no destination configured" | The toggle is on but the repository is blank |
| Submissions come back "not filed" | The token was refused, or a label does not exist in the repository. The submission is kept, with the reason, on the admin view |
| "Could not reach GitHub" behind a TLS proxy | The CA bundle is not in the container. See [Notifications](notifications.md), which has the same failure and the same fix — it is not a code change |
| Everything stops working after a key rotation | The token is encrypted with the same key as the rest of the stored configuration. Rotating `JWT_SECRET_KEY` means re-entering it |
| A guest sees no button | "Let evaluation guests submit" is off |
