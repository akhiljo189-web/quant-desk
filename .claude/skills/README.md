# Skills

Installed from the `superpowers` bundle (obra/superpowers), skills directory
only. Contents are verbatim from the upstream zip.

**What was NOT installed.** The bundle is a multi-agent *plugin*, and most of it
does not apply here: `hooks/`, `scripts/`, `.claude-plugin/`, `.codex-plugin/`,
`.cursor-plugin/` and friends only activate through a plugin manager, which is
not available in a cloud session. `CLAUDE.md` / `AGENTS.md` / `GEMINI.md` from
that repo were deliberately left out — they are its own project instructions and
would fight with this one's.

**Cross-references are namespaced and will not resolve.** The skills refer to
each other as `superpowers:brainstorming`, `superpowers:test-driven-development`
and so on — 26 references in total. That prefix is the plugin namespace. Loaded
as project skills they are named `brainstorming`, `test-driven-development`, …
so a reference like "invoke superpowers:brainstorming first" names nothing.
Invoke them by their bare names.

**`using-superpowers` changes how sessions behave.** It instructs the agent to
invoke a skill before *any* response, including clarifying questions. That is a
deliberate workflow choice, not a neutral addition — delete that one directory
if it is not wanted.
