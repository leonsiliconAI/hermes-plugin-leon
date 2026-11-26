# dropbox-task-memory

Part of the [leon plugin](../README.md), and a Hermes plugin of its own until 2026-11-26.

Hermes plugin for Hermes's per-issue task notes at Dropbox `/Agents/Memory/<linear-slug>.md`. With it, Hermes reads, saves and moves those notes without asking Leon to confirm every write. Anything else in Dropbox still needs his confirmation.

## Why

Leon keeps task memory for each Linear issue in Dropbox, one Markdown file per issue (the `branch-task-memory` skill on Captain Claude). Each save cost him a second "yes" in Slack. The prompt came from Dropbox's hosted MCP server (`mcp.dropbox.com`), not from Hermes and not from the OAuth app:

- Every write tool's description has an `ASSISTANT POLICY` section that begins: "Before calling this tool, the assistant MUST summarize the exact mutation plan and get explicit user confirmation in chat." That covers `create_file`, `create_folder`, `move`, `copy`, `delete`, `create_shared_link` and `restore_file_revision`. `restore_folder`, `create_file_request` and the Sign tools word their confirmation rules differently.
- [Dropbox's MCP help page](https://help.dropbox.com/integrations/connect-dropbox-mcp-server) documents no setting, header, query parameter or scope that changes this, and the rule doesn't vary by path. The Dropbox app only selects scopes.
- Hermes isn't adding a prompt of its own. Its MCP write gate applies only to servers marked `trust: untrusted`, and the `dropbox` server isn't. Its `approvals` settings cover terminal commands.

So the provider policy can't be configured. This plugin handles the one class of writes Leon authorized in advance (Slack, 2026-11-11). It uses the Dropbox HTTP API directly under a separate grant. The Dropbox MCP tools are unchanged and keep their confirmation rule for everything else.

## What it does

It registers four tools in the `dropbox_task_memory` toolset:

| Tool                                                   | Does                                                                                                                                                                                                                               |
| ------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `dropbox_task_memory_list`                             | Lists the notes in `/Agents/Memory`.                                                                                                                                                                                               |
| `dropbox_task_memory_read(slug)`                       | Returns a note's text and `rev`.                                                                                                                                                                                                   |
| `dropbox_task_memory_write(slug, content, mode, rev?)` | `create` fails if the note exists. `append` adds to the end and creates the note if it's missing. `replace` rewrites the note and needs the `rev` from a read, so it can't overwrite a newer version. Missing folders are created. |
| `dropbox_task_memory_move(source_path, slug)`          | Moves a file that is already under `/Agents/` to `/Agents/Memory/<slug>.md`. It fails if the destination exists and refuses folders.                                                                                               |

The limits are enforced in code, before any request goes out:

- `slug` must look like a Linear issue slug (`leon-330-prepare-separate-st-george-chapter-sop-application`). Folders, `..`, and anything other than lowercase letters, digits and hyphens are refused. The note path is always `/Agents/Memory/<slug>.md`.
- A move source must be a plain path inside `/Agents/`. No `id:` references, no `..`, no empty segments.
- No delete, sharing, restore, or file-request calls exist in the plugin. Writes use `autorename: false` and `strict_conflict: true`.
- The grant has only `files.metadata.read`, `files.content.read` and `files.content.write`. Even a bug can't share files or read account details.
- Content is capped at 512 KB.

Dropbox keeps earlier versions of every overwritten note, and each write result says so.

The write and move tool descriptions carry the authorization: Leon's dated standing rule, the `/Agents/Memory` scope, and a reminder that other Dropbox changes go through the MCP tools and their confirmation policy.

## Install

1. Get a refresh token for the plugin's own grant. Use a PKCE authorization on a Dropbox app with Full Dropbox access and public-client (PKCE) support. Captain Claude reuses Leon's "Hermes Captain Claude MCP" app (`z5alyoo8rmc0id7`), but this is a separate grant from the MCP server's. Request only the three scopes above and `token_access_type=offline`, and omit `redirect_uri` so Dropbox shows the code. Exchange the code with the verifier at `https://api.dropboxapi.com/oauth2/token`. You can do the exchange in the dashboard page, so the token never leaves it.
2. Put `DROPBOX_TASK_MEMORY_REFRESH_TOKEN` and `DROPBOX_TASK_MEMORY_APP_KEY` in `$HERMES_HOME/.env` (`PUT /api/env` on Hermes Cloud).
3. Install the [leon plugin](../README.md#install), which includes it.
4. Restart the gateway. The log shows `dropbox-task-memory: dropbox_task_memory_read, ... registered for /Agents/Memory`.
5. Tell Hermes to use it. On Captain Claude, the `branch-task-memory` skill and one line in `memories/MEMORY.md` say to save notes with these tools without asking, and to keep using the Dropbox MCP, with confirmation, for anything else.

To revoke it, remove the token from `.env` and the tools disappear (`check_fn`). To kill the grant itself, disconnect the app under Dropbox → Settings → Connected apps. That also disconnects the MCP server's grant for the same app, which then needs re-auth (see the `token_access_type=offline` note in the Hermes Dropbox MCP setup).

## Test

The feature imports nothing from Hermes. Its tests, `__tests__/test_dropbox_task_memory.py` in the [leon plugin](../README.md#test), run against an in-memory fake of the Dropbox API and run in CI.
