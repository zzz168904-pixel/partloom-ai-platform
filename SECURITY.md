# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| `0.1.0-beta.3` | Yes |
| `0.1.0-beta.2` | Yes |
| `0.1.0-beta.1` | No; historical MIT snapshot |
| Earlier snapshots | No |

## Secrets

Never commit `.env.local`, API keys, access tokens, customer documents, or
generated CAD files. The application reads provider credentials from the
current user's environment or a local, ignored `.env.local` file.

If a key is exposed:

1. Revoke or rotate it immediately in the provider console.
2. Remove it from local files and shell history.
3. Review provider usage logs.
4. Do not rely on deleting a Git commit to invalidate the key.

## Reports

Use the repository's private vulnerability reporting channel when available.
Do not place secrets, customer drawings, or proprietary CAD models in a public
issue.

## Local Automation

The gateway listens on localhost only. SolidWorks and AutoCAD automation uses
the interactive Windows user session. Do not expose the gateway to a public
network, and do not run untrusted CAD-IR without reviewing the execution plan.
