# Security

## Reporting

Report vulnerabilities through GitHub's private advisory form:
**Security → Report a vulnerability** on this repository. Please do not open a
public issue for anything exploitable.

Expect an acknowledgement within a few days. This is a volunteer project, so
there is no formal SLA and no bounty.

## Threat model

Knowing what Sieve does *not* defend against is more useful than a list of what
it does.

**There is no authentication.** Sieve is single-user by design and assumes
whoever can reach the port is the owner. Anyone who can open it can read your
watch history and change every setting. Keep it behind a VPN, an SSH tunnel, or
your reverse proxy's auth. The default bind is `127.0.0.1` for this reason, and
the Docker port mapping is likewise loopback-only.

**There is no multi-tenancy.** One database, one person. Do not put it in front
of a group.

Given those, the things actually in scope are:

| Surface | Defence |
|---|---|
| Imported profiles, from a file or a URL | Whitelisted in `profiles.sanitise_settings`: unknown keys dropped, values clamped, invalid rules discarded |
| Language-model output | Same whitelist, same code path |
| Rule expressions | Closed operator set over a flat namespace. No arithmetic, no function calls, no `eval`, bounded nesting depth |
| Upstream API responses | Parsed defensively; a malformed segment or branding entry is skipped, not trusted |
| SQL | Parameterised throughout. Table names in migrations are literals, never user input |
| Template output | Jinja autoescaping. Titles come from upstream and are never marked safe |
| Custom player templates | Scheme whitelist: http, https and named desktop players. `javascript:`, `data:` and `file:` are refused, in the Controls form, the API, and imported profiles alike |
| `/open/{id}` | Not an open redirect: the destination is built from your configured providers and a validated video id, never from the request |
| Reset | Requires the typed word `reset` from every surface, API included, and backs up first |

A profile from a stranger should not be able to make Sieve do anything the
Controls page cannot. If you find a way around that, it is a real bug and worth
reporting.

## Outbound requests

Sieve talks to four kinds of endpoint, all of which you configure:

- your Invidious instance
- `sponsor.ajay.app`, by four-character hash prefix, only if you enable DeArrow
  or SponsorBlock
- your own model endpoint, only if you configure one
- a profile URL, only when you paste one

There is no telemetry and no phone-home. The hash-prefix scheme means the
community APIs receive a bucket of a few hundred videos rather than the one you
asked about.

## Supported versions

The latest release on `main`. This project is young enough that there is nothing
older worth backporting to.
