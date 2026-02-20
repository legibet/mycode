# TODO

## Core (now)

- [ ] Add session log compaction (summarize older messages when context grows)
- [ ] Add safe path policy switch (restrict to cwd root vs allow full FS)
- [ ] Add better cancellation: cancel in-flight LLM request if provider supports it
- [x] Add basic tests for session store + tool truncation

## Skills (next)

- [ ] Define a minimal skill format (SKILL.md + scripts) and loader
- [ ] Add `/reload` or auto-discovery for skills

## UI (optional)

- [ ] Show token usage / cost if provider returns it
- [ ] Add copy / export session
