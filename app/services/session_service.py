import os
from dataclasses import dataclass, field

from app.agent.core import Agent


@dataclass
class SessionStore:
    sessions: dict[str, Agent] = field(default_factory=dict)

    def get_or_create(
        self,
        session_id: str,
        model: str,
        cwd: str,
        api_base: str | None,
    ) -> Agent:
        """Return existing agent or create a new one."""
        normalized_cwd = os.path.abspath(cwd)
        agent = self.sessions.get(session_id)
        if not agent or agent.model != model or agent.cwd != normalized_cwd:
            agent = Agent(model=model, cwd=normalized_cwd, api_base=api_base)
            self.sessions[session_id] = agent
        return agent

    def clear(self, session_id: str) -> None:
        """Clear conversation history for a session."""
        if session_id in self.sessions:
            self.sessions[session_id].clear()

    def get(self, session_id: str) -> Agent | None:
        """Get agent by session ID."""
        return self.sessions.get(session_id)
