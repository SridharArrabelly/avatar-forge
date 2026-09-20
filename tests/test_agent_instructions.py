"""Offline checks for publishing instruction-only updates on an existing agent."""

from __future__ import annotations

import copy
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import setup_foundry_agent as setup

SOURCE = {
    "kind": "prompt",
    "model": "unchanged-model",
    "instructions": "Previous instructions",
    "reasoning": {"effort": "none"},
    "tools": [{
        "type": "azure_ai_search",
        "azure_ai_search": {"indexes": [{
            "index_asset_id": "existing-index/versions/1", "query_type": "semantic", "top_k": 5,
        }]},
    }, {"type": "bing_custom_search_preview", "configuration": {"count": 8}}],
    "extra_settings": {"preserve": ["all", "values"]},
}


class Agents:
    def __init__(self, definition=None):
        self.latest = "8"
        self.definitions = {"8": copy.deepcopy(definition or SOURCE)}
        self.created = []
        self.concurrent = False
        self.reads = 0
        self.mutate_readback = False

    def get(self, name):
        self.reads += 1
        number = "concurrent" if self.concurrent and self.reads > 1 else self.latest
        return SimpleNamespace(versions=SimpleNamespace(latest=SimpleNamespace(version=number)))

    def get_version(self, name, number):
        return SimpleNamespace(
            version=number,
            definition=SimpleNamespace(as_dict=lambda: self.definitions[number]),
            description="Keep this description", metadata={"keep": "metadata"},
        )

    def create_version(self, agent_name, body):
        assert agent_name == "ExistingAgent"
        self.created.append(copy.deepcopy(body))
        self.latest = "9"
        self.definitions["9"] = copy.deepcopy(body["definition"])
        if self.mutate_readback:
            self.definitions["9"]["model"] = "unexpected"
        return SimpleNamespace(version="9")


def main() -> int:
    with patch.object(setup, "_load_prompt", return_value="New onboarding instructions"):
        agents = Agents()
        project = SimpleNamespace(agents=agents)
        result = setup.update_agent_instructions(project, "ExistingAgent")
        assert result.version == "9"
        assert agents.definitions["8"] == SOURCE
        assert agents.created == [{
            "definition": {**SOURCE, "instructions": "New onboarding instructions"},
            "description": "Keep this description", "metadata": {"keep": "metadata"},
        }]
        assert setup.update_agent_instructions(project, "ExistingAgent").version == "9"
        assert len(agents.created) == 1
        print("PASS only instructions change; original version and all other settings retained; idempotent")

        agents = Agents()
        agents.concurrent = True
        try:
            setup.update_agent_instructions(SimpleNamespace(agents=agents), "ExistingAgent")
        except RuntimeError as error:
            assert "changed while reading" in str(error)
        else:
            raise AssertionError("Concurrent update was not detected.")
        assert not agents.created
        agents = Agents()
        agents.mutate_readback = True
        try:
            setup.update_agent_instructions(SimpleNamespace(agents=agents), "ExistingAgent")
        except RuntimeError as error:
            assert "differs" in str(error)
        else:
            raise AssertionError("Unexpected readback was not detected.")
        print("PASS concurrent writes and unexpected readback fail explicitly")

    env = {
        "PROJECT_ENDPOINT": "https://example.invalid/projects/test",
        "AGENT_NAME": "ExistingAgent",
        "AI_SEARCH_TOP_K": "irrelevant-invalid-value",
    }
    with patch.dict(os.environ, env, clear=True), patch.object(setup, "load_dotenv"):
        settings = setup.load_settings(instructions_only=True)
        assert settings == {"project_endpoint": env["PROJECT_ENDPOINT"], "agent_name": "ExistingAgent"}
        context = Mock()
        context.__enter__ = Mock(return_value=SimpleNamespace(agents=Agents()))
        context.__exit__ = Mock(return_value=False)
        with patch.object(setup, "_credential", return_value=context), \
                patch.object(setup, "AIProjectClient", return_value=context), \
                patch.object(setup, "update_agent_instructions") as update, \
                patch.object(setup, "create_agent") as create, redirect_stdout(StringIO()):
            assert setup.main(["--update-instructions"]) == 0
            update.assert_called_once()
            create.assert_not_called()
        with redirect_stderr(StringIO()):
            try:
                setup.main(["--update-instructions", "--clone-from", "AnotherAgent"])
            except SystemExit as error:
                assert error.code == 2
            else:
                raise AssertionError("Conflicting modes were accepted.")
    print("PASS CLI needs only existing agent/project and cannot combine update/clone modes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
