---
name: analytics-mcp-release-notes
description: Use when the user asks to enhance, refine, polish, or "look at" the release notes for an adas-mcp (ai-dial-analytics-mcp) tag, typically a fresh CI-generated pre-release (e.g. `0.2.0-rc.0`) or a stable cut. Reads the auto-generated notes off the GitHub release, then classifies and rewrites each bullet in this project's editorial voice. Promotes MCP tool-surface and configuration breaks to `BREAKING CHANGES`, adds the `Deployment Changes` pointer when operators must act, and names the ADAS line the bundled OpenAPI snapshot targets. Saves a draft to `claude/release-notes/`. Never edits GitHub directly.
allowed-tools: Read Grep Glob LSP Bash(gh release view:*) Bash(gh release list:*) Bash(gh pr view:*) Bash(gh pr list:*) Bash(gh pr diff:*) Bash(gh api:*) Bash(git log:*) Bash(git show:*) Bash(git diff:*) Bash(git tag:*) Bash(git rev-parse:*) Bash(git cat-file:*) Bash(git ls-tree:*) Bash(git merge-base:*) Bash(git describe:*) Bash(date:*) Write(claude/release-notes/*) Bash(mkdir -p claude/release-notes) Bash(perl scripts/hygiene-sweep.pl:*)
argument-hint: "[tag]"
arguments: tag
model: opus
effort: xhigh
context: fork
agent: general-purpose
---

# adas-mcp release-notes enhancer

The CI publishes a GitHub release for every tag. The raw bullets come from `epam/ai-dial-ci`'s
`generate_release_notes` action, wired through `.github/workflows/release.yml` → `python_docker_release.yml`.
Know how it builds them, because every rewrite rule below corrects one of its habits:

- It takes the **commit subject lines** since a baseline tag. A pre-release or an incremental build uses the
  nearest tag on the branch. A stable promotion uses merge-base mode: everything since the last stable tag. With
  **no stable tag at all**, which is the case for the first GitHub release, it takes the **full history**.
- It de-duplicates subjects, sorts them **alphabetically**, and drops `[skip ci] Update version` and
  `Merge branch …` lines.
- It routes `feat` / `fix` / `docs` / `test` / `ci` / `chore` to `Features` / `Fixes` / `Docs` / `Tests` / `CI` /
  `Other`. It strips the type prefix and renders an optional scope as a leading parenthetical:
  `ci(deps): bump …` becomes `* (deps) bump …`.
- A `!` subject (`feat!:`, `fix!:`) is written **twice**, once under `BREAKING CHANGES` and once under its type
  section, with identical text.
- Any other type (`refactor`, `perf`, `build`, `revert`) and any non-conventional subject (`Initial commit`,
  `Revert "…"`, a hotfix pushed without a PR) goes to `Other` **verbatim, with the full subject**.
- Bullets carry whatever casing and phrasing the PR title had, plus the `(#<PR>)` GitHub appends to a squash
  commit. PR titles in this repo carry **no issue number**. The issue is linked in the PR body.

The release also sets the package version in `pyproject.toml` from the release line, so the committed `0.0.0` is
a placeholder: never read a version from `pyproject.toml`, read it from the tag.

This skill turns that raw list into notes an operator or an MCP-client integrator can read. You are running in a
forked, isolated context. Read and research freely: only the final summary you return reaches the main
conversation. The draft lands at `claude/release-notes/<tag>-draft.md`.

## Who reads these notes

Two audiences, and both care about different breaks than a backend's operators do:

- **Operators** deploying the `epam/ai-dial-analytics-mcp` image: env vars, the default port, the entrypoint and
  transport, the exposed ports, observability exporters, the Python base and image CVEs.
- **MCP-client integrators** (agent builders, DIAL Quick Apps, Claude Desktop/Code configs): the **tool
  surface**. A client binds tools **by name**, and an agent's prompt or allow-list names them too. A renamed,
  removed or newly gated `adas_*` tool, a renamed or newly required parameter, or a changed error-envelope
  `code` breaks a client exactly the way a removed endpoint breaks a REST caller.

## When to use

- "Enhance the release notes for `0.2.0-rc.0`"
- "Look at the latest pre-release notes and refine them"
- "The CI just published `<tag>`, make it readable"

Do **not** trigger on a recall question such as "what changed in 0.2?". That is not a notes-editing task.

## Inputs

`tag` = `$tag`, the GitHub release tag to enhance (e.g. `0.2.0-rc.0`, `0.2.0`). If it is empty, pick the most
recent tag from `gh release list --limit 5` and confirm it with the user before editing.

## Workflow

### 1. Resolve the target, the range and the style anchors

1. `gh release view <tag> --json body,name,tagName,isPrerelease`: capture the raw CI notes.
2. `gh release list --limit 10` and `git tag --list | sort -V`: locate the previous tag of the same kind. For a
   stable release that is the last stable tag. For a delta `rc.N+1` it is the predecessor `rc.N`.
3. **First release.** If no earlier tag exists, the range is the full history. Expect an `Initial commit`
   bullet and every PR since. Say so in the editorial notes (§8).
4. **Style anchors.** If earlier releases exist, read the previous stable body and, when relevant, the previous
   rc body (`gh release view <prev> --json body`). They are the anchors, provided a human already edited them.
   If none exist yet, the rules in this skill are the house style. The house style is **terse: one line per
   bullet**, issue ref first, PR ref last, no multi-paragraph descriptions. Match that terseness, not your own
   instincts. The sibling repos' releases (`epam/ai-dial-analytics`, `epam/ai-dial-analytics-enrichment-runner`)
   follow the same house style and are fair anchors while this repo has none of its own.
5. `git log <prev-tag>..<tag> --oneline`, or `git log <tag> --oneline` for a first release: the full commit list.
   It shows hotfix commits pushed without a PR, revert pairs, and the real chronology behind the alphabetical
   CI order.

### 2. Pull source context for each bullet

For every bullet in the raw notes:

1. Parse the trailing `(#<PR>)`. `gh pr view <PR> --json title,body,labels` returns the body. Read the body,
   not just the title: the *what changed* lives there, and the title is usually too compressed.
2. **Find the issue ref in the PR body.** The template's `### Applicable issues` section lists it as
   `fixes #<n>`, `closes #<n>`, `resolves #<n>` or a bare `#<n>`. **Ignore the template's HTML comment**: it
   contains the placeholder `#123`, which is not an issue. `- fixes #` with no number, `- N/A` or an empty
   section means no issue. Several issues → `#<a>, #<b>`. Never guess an issue from a branch name or the PR
   title.
3. Bullets with no `(#<PR>)` were pushed straight to `development` or a `release-*` branch. Find the commit
   with `git log <range> --oneline | grep -i <keywords>` and `git show <hash>`. These are usually hotfixes or
   reverts that fold into a related entry (§4) rather than stand alone.
4. A PR body that names a doc under `docs/` (`tools.md`, `configuration.md`, `deployment-guide.md`,
   `observability.md`) gives you the headline framing. Skim it when the PR body alone doesn't tell you what a
   user sees.

### 3. Cross-check the deployment- and client-relevant sources

The `BREAKING CHANGES` entries, the `Deployment Changes` section and the ADAS pairing are built from primary
sources, not PR titles:

- **Tool surface.** `git diff <prev-tag>..<tag> -- tests/test_tools_smoke.py`: the `EXPECTED_LIMITED`,
  `EXPECTED_FULL_ONLY` and `EXPECTED_EXECUTE_SQL` sets pin the registered surface exactly. A name leaving a set
  is a removal or rename; a name moving from `EXPECTED_LIMITED` to `EXPECTED_FULL_ONLY` (or behind a flag) is a
  newly gated tool. Both are breaking. A name only added is a feature. Confirm against
  `src/adas_mcp/tools/__init__.py` (`register_all`) and the `register` / `register_full` calls at the tag, and
  against the tool counts in `CLAUDE.md` and `docs/tools.md`.
- **Tool signatures.** `git diff <prev-tag>..<tag> -- src/adas_mcp/tools/`: a renamed parameter, a parameter
  that became required, a changed wire translation (`date_from` → `from`) or a changed default is a client break
  even when the tool name stays. Check the `async def adas_*` signatures, not the docstrings.
- **Error envelope and normalization.** `git diff <prev-tag>..<tag> -- src/adas_mcp/errors.py
  src/adas_mcp/client.py`: a changed `status_to_code` mapping or a changed `_parse_response` reshape
  (`{items: [...]}`, `{ok: True}`, `{value: ...}`) changes what every client parses. Also an auth-precedence
  change in `client._build_headers` / `auth.py`: a new or reordered credential source is an operator action.
- **Configuration.** `git diff <prev-tag>..<tag> -- src/adas_mcp/config.py src/adas_mcp/server.py
  docs/configuration.md README.md`: env-var additions, removals, renames, accepted values and default changes.
  The operator-facing token is the **env var** (`ADAS_*`, `FASTMCP_*`, `OTEL_*`). Verify it in the code
  (`os.environ.get(...)` in `config.py` / `server.py`), then confirm it against the variable's row in
  `docs/configuration.md`. A PR body may cite a pre-rename name. The code wins, and the default is the one the
  code passes to `os.environ.get`, not the one a PR describes.
- **Image and runtime.** `git diff <prev-tag>..<tag> -- Dockerfile pyproject.toml .dockerignore`: a changed
  `ENTRYPOINT` / `CMD` (the default transport), `EXPOSE`d ports, baked-in `ENV` defaults (`FASTMCP_PORT`,
  `OTEL_*_EXPORTER`), the base image or `requires-python`, the `otel` extra. A port, transport or entrypoint
  change is an operator action.
- **Observability.** `git diff <prev-tag>..<tag> -- src/adas_mcp/telemetry.py docs/observability.md`: renamed
  spans, metrics or attributes break dashboards and alerts.
- **ADAS pairing.** `git log --oneline <prev-tag>..<tag> -- src/adas_mcp/swagger/adas-openapi.json`: a commit
  here means the bundled OpenAPI snapshot was refreshed. The snapshot's `info.version` is the API version
  (`v1`), not the ADAS release, so take the ADAS tag from the PR body (the refresh PR names it, e.g. "from ADAS
  `<X.Y.Z-rc.N>`'s `docs/openapi.json`"). Confirm the tag exists with
  `gh release list -R epam/ai-dial-analytics --limit 20`. See §6 for when it becomes a pairing line.
- **Upgrade docs.** `git ls-tree -r --name-only <tag> docs/`: the docs the tag actually ships. At the time this
  skill was written the repo had no per-release upgrade doc. If one appears (the siblings use
  `docs/release-<X.Y>-upgrade.md`), link it per §6.

### 4. Classify each bullet

The CI sorts by conventional-commit *type*, which says little about user impact. **Keep its section structure**
(`BREAKING CHANGES`, `Features`, `Fixes`, `Docs`, `Tests`, `CI`, `Other`): a reader is better served by dedicated
`Docs` / `Tests` / `CI` headings than by one overflowing `Other`. Move items by what they do:

| Where CI put it | Where it belongs | Rule |
|---|---|---|
| `BREAKING CHANGES` + `Features`/`Fixes` (a `!` commit) | **both**, worded differently | `BREAKING CHANGES` states what an operator or client must do. `Features`/`Fixes` keeps the plain change line. See §5 rule 2. |
| `Other`, a `chore:` that changes runtime behavior or a default | `Features` / `Fixes` | E.g. a default port, a security or CVE bump in the image, a behavior correction. |
| A breaking change not flagged with `!` | also under `BREAKING CHANGES` | A renamed, removed or newly gated `adas_*` tool, a renamed or newly required tool parameter, a changed error-envelope `code` or response shape, an env-var rename or removal, a changed default transport, port or entrypoint, a dropped Python version. Verify it from source (§3). |
| `Other`, a `refactor`/`perf`/`build` subject | `Fixes`, `Features` or `Other` | By user impact. A visible perf gain is a `Fixes` or `Features` line, an internal refactor stays in `Other`. |
| `Features`/`Fixes` for a regression introduced and fixed inside this pre-release line | `Fixes` with "(affects pre-release users only)" | Don't surface a transient bug as a feature. |
| A docstring-only change to a tool | `Features` or `Fixes`, not `Docs` | Tool docstrings are the description every MCP client shows its model: they are shipped API, not documentation. A docstring that now teaches a new ADAS field is a feature; one that corrected wrong guidance is a fix. |
| Genuine docs / test / CI-workflow work | leave it in `Docs` / `Tests` / `CI` | `README.md` and `docs/**` are `Docs`. Tidy the wording, keep the section. |
| Several PRs or hotfix commits on one change | one folded entry in the right section | Cite every PR `(#a, #b)` or commit hash. |
| A commit and its `Revert "…"` both in range | drop both | Net effect is zero. Say so in the editorial notes. |
| A later PR in the range undoes or reshapes an earlier one | one entry with the **net** effect | Don't announce a change the release doesn't ship. Cite both PRs. |

**Drop from the notes entirely.** These are the only items with zero signal for any reader:

- `Merge branch …` / `Merge remote-tracking branch …` and `[skip ci] Update version`. The CI strips these, so
  flag any that slipped through.
- `Initial commit` on a first release. It is the import of the code base, not a change.
- Revert pairs with zero net effect, and exact duplicates folded into another entry.

**Keep everything else, filed under its real section.** Test work goes to `Tests`, documentation to `Docs`, and CI
workflow changes to `CI`. `Other` takes the rest: Claude Code tooling (skills, `CLAUDE.md`), the hygiene sweep,
dependency and GitHub-Actions bumps, internal refactors. When unsure whether something is worth a line, keep it,
and raise the borderline call in the editorial notes (§8) instead of dropping it silently.

### 5. Rewrite each kept bullet

The target form is:

```
* #<issue> <Active-voice description of what changed> (#<PR>)
```

Rules in order of importance:

1. **One line per bullet.** No multi-paragraph descriptions. If more detail matters, put it in the editorial-notes
   file (§8), not the draft.
2. **A breaking change gets two lines, not one line twice.** Under `BREAKING CHANGES`, say what breaks and what
   to do: "`adas_list_tables` is renamed `adas_list_catalog_tables`. Update client allow-lists and prompts that
   name it". Under `Features`/`Fixes`, keep the plain description of the change. Both carry the same refs.
3. **Lead with the issue ref, end with the PR ref:** `#<issue> … (#<PR>)`. Multiple PRs → `(#<a>, #<b>)`.
   Multiple issues → `#<a>, #<b> …`. No issue → start with the description and keep `(#<PR>)` at the end. Orphan
   commits → short hashes in backticks, `` (`a1b2c3d`) ``. Never strip a ref: the numbers auto-link on the
   release page.
4. **Fold the scope parenthetical into the prose.** `* (deps) bump the ai-dial-ci group …` becomes "Bump the
   `ai-dial-ci` workflow group …". A leading `(scope)` never survives.
5. **Keep it terse.** A bullet states *what changed*, not *why*. Add a short clarifying clause only when it
   materially helps a reader.
6. **Quote code identifiers in backticks**: tool names (`adas_preview_pipeline`), tool parameters (`body`,
   `view`), env vars (`ADAS_CAPABILITIES`), capability values (`full`), ADAS endpoints (`/v1/pipelines`),
   error codes (`invalid_argument`), headers (`Api-Key`), ports, transports (`streamable-http`).
7. **Name the gate when a tool is added.** A new tool says where it registers: "(`full` only)", "(behind
   `ADAS_ENABLE_EXECUTE_SQL`)", or nothing when it is read-only and always registered.
8. **Mark preview features** with an inline `(preview)` suffix, and regressions restoring previously working
   behavior with `(regression fix)`.
9. **Quote CVE IDs verbatim** for image or dependency security upgrades, and keep the dependency and version
   detail.
10. **Re-order from the CI's alphabetical sort to a logical grouping.** Within each section, lead with the
    headline changes. Then group by the tool groups in `CLAUDE.md`'s order: schema introspection, query
    discovery and SQL, the table catalog, data-sync backfill and failure ledger, pipelines, catalog promotion,
    saved queries and dashboards, instance settings. Then transport and configuration, auth, observability, the
    image. Trail dependency and tooling bumps.
11. **Capitalize the first word** of the description. The CI keeps the PR title's lower case.
12. **Public-repo hygiene applies to the draft.** These notes are public. Follow the "Public-repo hygiene" rules in
    `CLAUDE.md`: no environment attribution, no operational history, no real-data figures, no internal design
    ids, no machine-specific paths. PR bodies sometimes carry these. Never copy them into a bullet. After
    writing the draft, sweep it with the repo's canonical patterns: `perl scripts/hygiene-sweep.pl - <
    claude/release-notes/<tag>-draft.md` (exit 0 = clean, 1 = hits to judge). Fix every real hit; note any
    dismissed one in the editorial notes.

#### Example transformations

Each pair is `raw CI` → `enhanced`. They are illustrative: PR and issue numbers, and tool and env-var names not in
the repo, are invented.

```
# Capitalize, take the issue from the PR body ("fixes #12"), backtick the identifiers, name the gate:
- * add tool to list pipeline runs (#31)
+ * #12 Add `adas_list_pipeline_runs` to read a pipeline's run history (#31)
- * add requeue for data sync failures (#33)
+ * Add `adas_requeue_sync_failures` to requeue failed data-sync batches (`full` only) (#33)

# A breaking change: the CI duplicates it. Reword the BREAKING CHANGES line as the action to take:
- ## BREAKING CHANGES
- * change default port to 5000 (#3)
- ## Features
- * change default port to 5000 (#3)
+ ## BREAKING CHANGES
+ * The default streamable-http port moves from `8000` to `5000`. Update `containerPort`, probes and client URLs, or set `FASTMCP_PORT` to keep the old port (#3)
+ ## Features
+ * Serve streamable-http on port `5000` by default (#3)

# A tool moved behind a gate with no `!`, found in the smoke-test sets (§3): promote it:
- * gate preview behind full capability (#40)
+ ## BREAKING CHANGES
+ * `adas_preview_pipeline` now registers only under `ADAS_CAPABILITIES=full`; a `limited` deployment no longer lists it (#40)

# An ADAS alignment: say which ADAS line the tools now describe:
- * align with ADAS 0.24 catalog access and chart legend (#4)
+ * Align with ADAS `0.24`: catalog promotion tools take `include_access`, and saved queries and dashboards document the chart legend (#4)

# Fold the scope parenthetical, and fold repeated dependency bumps into one line:
- ## CI
- * (deps) bump the ai-dial-ci group with 3 updates (#1)
- * (deps) bump the ai-dial-ci group with 2 updates (#7)
+ ## CI
+ * Bump the `ai-dial-ci` workflow group (#1, #7)

# A commit pushed by mistake and its revert: both go, and the editorial notes say why:
- * Revert "fix: retry ADAS calls on 503" This reverts commit …
- * fix: retry ADAS calls on 503
+ (dropped: reverted in the same range)
```

Always read the real tool and env-var names from the code at the tag, never from these examples.

### 6. Build the `Deployment Changes` section and the ADAS pairing line

`Deployment Changes` exists only when the range carries something an operator or a client integrator must act on:
an env-var addition, rename or removal, a changed default, a port, transport or entrypoint change, a renamed,
removed or newly gated tool, a changed error-envelope or response shape, or a minimum ADAS version. It is **a
pointer, not a table**:

```markdown
## Deployment Changes

This release includes <changes that need operator action | configuration changes>. Please review the [configuration reference](https://github.com/epam/ai-dial-analytics-mcp/blob/<tag>/docs/configuration.md) before proceeding.

Requires ADAS <X.Y> or later: the bundled OpenAPI snapshot and the tool descriptions follow ADAS `<ADAS-tag>`. See the [ADAS release](https://github.com/epam/ai-dial-analytics/releases/tag/<ADAS-tag>).
```

To produce it:

1. **Pick the link.** If the tag ships a per-release upgrade doc (`docs/release-<X.Y>-upgrade.md`, one per minor
   line, as in the sibling repos), link that. Otherwise link the doc that covers the change:
   `docs/configuration.md` for env vars and capabilities, `docs/deployment-guide.md` for ports, probes and the
   image, `docs/tools.md` for the tool surface. Confirm the file exists **at the tag**:
   `git cat-file -e <tag>:<path>`.
2. **Never link a file the tag doesn't ship.** A git-ignored working draft is absent from the
   tag. If the range has changes that need operator action and no tracked doc covers them, don't invent one and
   don't link the draft. Write the section without the link sentence, flag it as an open item in the editorial
   notes, and list what a doc must cover (§3 findings). Several breaking changes in one minor line are a reason
   to propose a `docs/release-<X.Y>-upgrade.md`; say so there.
3. **Pick the wording from what the change demands.** Use "changes that need operator action" when there are
   renamed or removed env vars, a port, transport or entrypoint change, a renamed, removed or newly gated tool,
   or a required ADAS upgrade. Use "configuration changes" when there are only new optional env vars or changed
   defaults.
4. **Pin the link to a tag that has the file.** A stable release points at its own tag. Verify the path resolves.
5. **ADAS pairing line: only when the snapshot was refreshed in the range (§3) _and_ a tool now depends on
   something the older ADAS lacks** — a new endpoint, a new body member, a new query parameter. ADAS reads bodies
   strictly, so a tool sending a member an older ADAS doesn't know fails with 400 there. A refresh that only
   picked up documentation or unrelated DTOs does not get a line; mention it in the editorial notes instead.
   Pair stable with stable where an ADAS stable carrying the change exists; otherwise name the rc the PR body
   cites. If the ADAS tag named in the PR body has no GitHub release, name the version line, leave out the link,
   and flag it as open.
6. **Do not inline env-var tables, Helm values, Kubernetes manifests or client config snippets** in the release
   body. That detail lives in `docs/`.

Leave out the whole section for a range with nothing to act on. Most delta `rc`s have nothing.

### 7. Pre-release and delta handling

If the target is `<X.Y.Z>-rc.N` with `N ≥ 1`:

- The release covers only what changed since the previous rc. Do **not** consolidate or rewrite the
  predecessor's notes: each pre-release tag has its own release page, and consolidation happens at the stable cut.
- Leave out sections with no entries. A small rc is often a single section.
- Do **not** prepend a "Delta since <prev-rc>" header. The `-rc.N` suffix already says what the release is.

A stable promotion (`X.Y.0`) is generated in merge-base mode, so its raw notes already span the whole line
since the last stable. Enhance it as one release. Don't paste the rc drafts together.

### 8. Save the draft and the optional editorial companion

Create `claude/release-notes/` if it is missing (it is git-ignored), then write:

- **`claude/release-notes/<tag>-draft.md`**: the final notes, ready to paste into the GitHub release body. No
  preamble, no commentary, just headings and bullets.
- **`claude/release-notes/<tag>-editorial-notes.md`** *(optional)*: only when there are non-obvious calls to
  surface:
  - a raw → enhanced mapping for non-trivial rewrites;
  - dropped or folded items, with a one-line reason each;
  - open questions (e.g. "Keep the `harden-runner` bump under CI?");
  - every place a source of truth disagreed with a PR body: a tool name, an env-var name or default, a gate, a
    missing doc, an ADAS tag the PR named but GitHub doesn't carry.

### 9. Verify nothing was pushed to GitHub

This skill **never** runs `gh release edit`, `gh release create`, a `gh api` call with `-X`/`--method`/`-f`/`-F`,
or any other write against GitHub. Everything is drafted in local files under `claude/release-notes/`. Applying
the draft is a separate, explicit request.

## Output format

`claude/release-notes/<tag>-draft.md` follows this shape:

```markdown
## BREAKING CHANGES

* <what breaks and what to do> (#<PR>)

## Features

* #<issue> <one bullet per change> (#<PR>)

## Fixes

* #<issue> <one bullet per change> (#<PR>)

## Docs

* <documentation changes>

## Tests

* <test additions / migrations>

## CI

* <CI / workflow changes>

## Other

* <chores and anything that fits nowhere above: Claude Code tooling, dependency bumps, internal refactors>

## Deployment Changes

This release includes <wording>. Please review the [<doc name>](https://github.com/epam/ai-dial-analytics-mcp/blob/<tag>/docs/<doc>.md) before proceeding.

<ADAS pairing line, only when §6 rule 5 applies>
```

Section order follows the CI: `BREAKING CHANGES` → `Features` → `Fixes` → `Docs` → `Tests` → `CI` → `Other` →
`Deployment Changes`. Emit only the sections that have entries.

## Return to the main conversation

Return a short summary of five lines or fewer:

- the draft path (`claude/release-notes/<tag>-draft.md`);
- bullet counts per section after enhancement;
- reclassifications (e.g. "promoted 1 newly gated tool to BREAKING CHANGES, moved 1 docstring fix out of Docs");
- items dropped or folded (count, one example);
- whether `Deployment Changes` was added, what it links to, and whether an ADAS pairing line was added;
- open questions (a source/PR-body disagreement, a missing doc, an ambiguous category).

Example:

> Drafted `claude/release-notes/0.1.0-draft.md`. 1 BREAKING CHANGES, 2 Features, 2 CI. First release: dropped `Initial commit`, folded 2 `ai-dial-ci` bumps. Deployment Changes links `blob/0.1.0/docs/configuration.md` and requires ADAS 0.24 (snapshot refreshed from `0.24.0-rc.0`, catalog promotion sends `include_access`). One open: no ADAS stable carries `0.24` yet, so the pairing names the rc.

## Safety rails

- **Never edit GitHub.** No `gh release edit` or `gh release create`, and no `gh api` call other than a GET. Drafts only.
- **Never invent items.** Every kept bullet maps to a PR or a commit in the range.
- **Never silently drop or rename a PR or issue ref.** Each bullet keeps `#<issue> … (#<PR>)` so the links
  resolve.
- **Verify tool names and gates from `tests/test_tools_smoke.py` and `register_all`, and env-var names and
  defaults from `config.py` / `server.py`**, not from PR bodies.
- **Don't inline doc detail** into the release body. `Deployment Changes` is a pointer.
- **Never link an untracked or missing file.** Check it exists at the linked tag.
- **Don't consolidate pre-release notes** into the stable's notes unless the user explicitly asks.
- **Follow the public-repo hygiene rules** in every line of the draft.

## Maintenance

Conventions drift. A pattern this skill doesn't handle may turn up in the raw notes: a new commit type, a new
section from `epam/ai-dial-ci`, a rewrite the user keeps asking for, or the first per-release upgrade doc. If you
notice one, mention it in your return summary and offer to update this `SKILL.md`. The user confirms before any
edit lands. The sibling repos carry the same skill — `analytics-release-notes` in `ai-dial-analytics` and
`enrichment-runner-release-notes` in `ai-dial-analytics-enrichment-runner`. Keep the shared parts (the CI
behavior, classification, bullet form, safety rails) in step with them.
