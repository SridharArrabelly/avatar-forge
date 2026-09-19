"""Offline checks for retrieval configuration and guarded, retrieval-only clones.

Run from the repository root: python tests/test_agent_retrieval.py
All project/credential operations are fakes; no Azure calls or files are created.
"""
from __future__ import annotations

import copy
import os
import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import setup_foundry_agent as agent


ENV = {
    "PROJECT_ENDPOINT": "https://example.invalid/api/projects/test",
    "AGENT_NAME": "evaluation-agent",
    "SEARCH_INDEX_NAME": "whole-sections-v1",
    "SEARCH_CONNECTION_NAME": "search",
    "AGENT_MODEL": "gpt-5.4",
}
CLONE_ENV = {key: ENV[key] for key in ("PROJECT_ENDPOINT", "AGENT_NAME", "SEARCH_INDEX_NAME")}
SOURCE = {
    "kind": "prompt",
    "model": "production-deployment",
    "instructions": "Exact live prompt, including {{unchanged}} placeholders.\n",
    "reasoning": {"effort": "none", "summary": "auto"},
    "text": {"format": {"type": "text"}},
    "tools": [
        {
            "type": "bing_custom_search_preview",
            "bing_custom_search_preview": {"search_configurations": [{
                "project_connection_id": "/connections/production-bing",
                "instance_name": "production-sites", "count": 3,
                "market": "en-ZA", "set_lang": "en", "freshness": "Month",
            }]},
        },
        {
            "type": "azure_ai_search",
            "azure_ai_search": {"indexes": [{
                "project_connection_id": "/connections/production-search",
                "index_name": "production-index", "query_type": "vector_simple_hybrid",
                "top_k": 8, "filter": "category eq 'minutes'",
            }]},
        },
        {"type": "code_interpreter", "container": {"type": "auto"}},
    ],
    "additional_settings": {"nested": ["must", "survive"]},
}


@contextmanager
def raises(error_type, text: str):
    try:
        yield
    except error_type as exc:
        assert text in str(exc), str(exc)
    else:
        raise AssertionError(f"Expected {error_type.__name__}: {text}")


def version(name, definition):
    data = copy.deepcopy(definition)
    return SimpleNamespace(
        id=f"{name}:1", name=name, version="1",
        definition=SimpleNamespace(as_dict=lambda: data),
        description="Unchanged source description", metadata={"purpose": "test"},
    )


class Agents:
    def __init__(self, source=SOURCE, target=None, on_create=None):
        self.versions = {"production-agent": version("production-agent", source)}
        if target is not None:
            self.versions["evaluation-agent"] = version("evaluation-agent", target)
        self.created = []
        self.reads = []
        self.on_create = on_create
        self.get_errors = {}

    def get(self, name):
        self.reads.append(("get", name))
        if name in self.get_errors:
            raise self.get_errors[name]
        if name not in self.versions:
            raise ResourceNotFoundError(f"Agent {name!r} not found")
        return SimpleNamespace(versions=SimpleNamespace(latest=self.versions[name]))

    def get_version(self, name, number):
        self.reads.append(("get_version", name, number))
        result = self.versions[name]
        assert result.version == number
        return result

    def create_version(self, agent_name, body=None, **kwargs):
        assert agent_name == "evaluation-agent", "Source must never be written"
        if body is None:
            body = {**kwargs, "definition": kwargs["definition"].as_dict()}
        self.created.append(copy.deepcopy(body))
        created = version(agent_name, body["definition"])
        self.versions[agent_name] = created
        if self.on_create:
            self.on_create(self)
        return created


class Project:
    def __init__(self, **kwargs):
        self.agents = Agents(**kwargs)
        self.connections = SimpleNamespace(
            get=Mock(return_value=SimpleNamespace(id="/connections/search")),
            list=Mock(return_value=[]),
        )
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


def clone_settings():
    with patch.dict(os.environ, CLONE_ENV, clear=True), patch.object(agent, "load_dotenv"):
        return {
            **agent.load_settings(clone_from="production-agent"),
            "ai_search_query_type": "semantic", "ai_search_top_k": 5,
        }


def main():
    with patch.dict(os.environ, ENV, clear=True), patch.object(agent, "load_dotenv") as loader:
        settings = agent.load_settings()
        loader.assert_called_once_with()
        assert settings["ai_search_query_type"] == "semantic"
        assert settings["ai_search_top_k"] == 5
        index = agent.build_tools("/connections/search", "index")[0].as_dict()["azure_ai_search"]["indexes"][0]
        assert index["query_type"] == "semantic"
        assert index["top_k"] == 5
    with patch.dict(os.environ, {key: value for key, value in ENV.items() if key != "AGENT_MODEL"}, clear=True), patch.object(agent, "load_dotenv"):
        assert agent.load_settings()["agent_model"] == "gpt-5.6-terra"
    with patch.dict(os.environ, {**ENV, "AI_SEARCH_QUERY_TYPE": "vector_simple_hybrid", "AI_SEARCH_TOP_K": "8"}, clear=True):
        index = agent.build_tools("/connections/search", "index")[0].as_dict()["azure_ai_search"]["indexes"][0]
        assert (index["query_type"], index["top_k"]) == ("vector_simple_hybrid", 8)
    print("PASS Terra and semantic/top-5 defaults; explicit legacy retrieval remains supported")

    for query_type in ("simple", "semantic", "vector", "vector_simple_hybrid", "vector_semantic_hybrid"):
        with patch.dict(os.environ, {**ENV, "AI_SEARCH_QUERY_TYPE": query_type, "AI_SEARCH_TOP_K": "5"}, clear=True), patch.object(agent, "load_dotenv"):
            settings = agent.load_settings()
            project = Project()
            created, web = agent.create_agent(project, settings)
            index = created.definition.as_dict()["tools"][0]["azure_ai_search"]["indexes"][0]
            assert (index["query_type"], index["top_k"]) == (query_type, 5)
            assert not web
    # Explicitly loaded settings must not be replaced by later ambient changes.
    with patch.dict(os.environ, {"AI_SEARCH_QUERY_TYPE": "simple", "AI_SEARCH_TOP_K": "99"}, clear=True):
        created, _ = agent.create_agent(Project(), {**settings, "ai_search_query_type": "semantic", "ai_search_top_k": 5})
        index = created.definition.as_dict()["tools"][0]["azure_ai_search"]["indexes"][0]
        assert (index["query_type"], index["top_k"]) == ("semantic", 5)
    print("PASS all SDK query types are supported; semantic/top-5 reaches the published tool")

    for key, values in (
        ("AI_SEARCH_QUERY_TYPE", ("", " ", "hybrid", "SEMANTIC")),
        ("AI_SEARCH_TOP_K", ("", " ", "0", "-1", "five", "5.0")),
    ):
        for value in values:
            with patch.dict(os.environ, {**ENV, key: value}, clear=True), patch.object(agent, "load_dotenv"):
                with raises(ValueError, key):
                    agent.load_settings()
                project = Project()
                with raises(ValueError, key):
                    agent.create_agent(project, {k: v for k, v in settings.items() if not k.startswith("ai_search_")})
                project.connections.get.assert_not_called()
                assert not project.agents.created
                with raises(ValueError, key):
                    agent.build_tools("/connections/search", "index")
                credential = Mock()
                with patch.object(agent, "_credential", credential), redirect_stderr(StringIO()):
                    with raises(SystemExit, "2"):
                        agent.main([])
                credential.assert_not_called()
    for value in (None, True, 1.5, 0):
        project = Project()
        with raises(ValueError, "AI_SEARCH_TOP_K"):
            agent.create_agent(project, {**settings, "ai_search_top_k": value})
        project.connections.get.assert_not_called()
        assert not project.agents.created
    print("PASS invalid and blank retrieval settings fail before credentials, connections, or publication")

    env_file = Path("private-evaluation.env")
    contents = (
        "AGENT_NAME=isolated-target\nSEARCH_INDEX_NAME=versioned-sections\n"
        "AI_SEARCH_QUERY_TYPE=semantic\nAI_SEARCH_TOP_K=5\n"
    )
    with patch.dict(os.environ, {**ENV, "AI_SEARCH_QUERY_TYPE": "vector", "AI_SEARCH_TOP_K": "8", "PYTHON_DOTENV_DISABLED": "1"}, clear=True):
        with patch.object(Path, "is_file", return_value=True), patch.object(Path, "open", return_value=StringIO(contents)) as opened:
            explicit = agent.load_settings(env_file)
            opened.assert_called_once_with(encoding="utf-8")
        assert explicit["agent_name"] == "isolated-target"
        assert explicit["search_index_name"] == "versioned-sections"
        assert explicit["ai_search_query_type"] == "semantic" and explicit["ai_search_top_k"] == 5
        assert explicit["agent_model"] == ENV["AGENT_MODEL"]
    with patch.object(Path, "is_file", return_value=False), patch.object(agent, "load_dotenv") as loader:
        with raises(FileNotFoundError, "Explicit env-file"):
            agent.load_settings(env_file)
        loader.assert_not_called()
        with patch.object(agent, "_credential") as credential, redirect_stderr(StringIO()):
            with raises(SystemExit, "2"):
                agent.main(["--env-file", str(env_file)])
            credential.assert_not_called()
    for key in ("AI_SEARCH_QUERY_TYPE", "AI_SEARCH_TOP_K"):
        with patch.dict(os.environ, {**ENV, "AI_SEARCH_QUERY_TYPE": "semantic", "AI_SEARCH_TOP_K": "5"}, clear=True):
            with patch.object(Path, "is_file", return_value=True), patch.object(Path, "open", return_value=StringIO(f"{key}\n")):
                with raises(ValueError, key):
                    agent.load_settings(env_file)
    print("PASS explicit env-file overlays supplied keys and missing files fail")

    settings = clone_settings()
    assert settings["agent_model"] is None and settings["search_connection_name"] is None
    assert settings["bing_connection_name"] is None
    expected = copy.deepcopy(SOURCE)
    expected["tools"][1]["azure_ai_search"]["indexes"][0].update(
        index_name="whole-sections-v1", query_type="semantic", top_k=5,
    )
    project = Project()
    created = agent.clone_agent(project, settings, "production-agent")
    assert created.definition.as_dict() == expected
    assert project.agents.created == [{
        "definition": expected, "description": "Unchanged source description", "metadata": {"purpose": "test"},
    }]
    assert project.agents.versions["production-agent"].definition.as_dict() == SOURCE
    assert project.agents.reads.count(("get", "production-agent")) == 2
    assert project.agents.reads.count(("get_version", "production-agent", "1")) == 2
    assert ("get_version", "evaluation-agent", "1") in project.agents.reads
    project.connections.get.assert_not_called()
    project.connections.list.assert_not_called()
    again = agent.clone_agent(project, settings, "production-agent")
    assert again.version == created.version and len(project.agents.created) == 1
    search_only = {**SOURCE, "tools": SOURCE["tools"][1:2]}
    project = Project(source=search_only)
    with patch.dict(os.environ, {"AGENT_MODEL": "ignored", "AGENT_REASONING_EFFORT": "high", "BING_COUNT": "99"}, clear=True):
        with patch.object(agent, "_load_prompt", side_effect=AssertionError("Clone must not rebuild the prompt")):
            cloned = agent.clone_agent(project, settings, "production-agent")
    assert cloned.definition.as_dict() == {**expected, "tools": expected["tools"][1:2]}
    print("PASS clone preserves every other field and all source connections; exact matches are idempotent")

    project = Project(target=SOURCE)
    with raises(RuntimeError, "different definition"):
        agent.clone_agent(project, settings, "production-agent")
    assert not project.agents.created
    for source_name in ("evaluation-agent", " EVALUATION-AGENT ", ""):
        project = Project()
        with raises(ValueError, "source agent"):
            agent.clone_agent(project, settings, source_name)
        assert not project.agents.reads
    for key, value in (("agent_name", None), ("search_index_name", ""), ("ai_search_query_type", ""), ("ai_search_top_k", 0)):
        project = Project()
        with raises(ValueError, key.upper()):
            agent.clone_agent(project, {**settings, key: value}, "production-agent")
        assert not project.agents.reads
    with patch.dict(os.environ, CLONE_ENV, clear=True), patch.object(agent, "load_dotenv"):
        with raises(ValueError, "must differ"):
            agent.load_settings(clone_from=CLONE_ENV["AGENT_NAME"])
    for change in ("multiple_indexes", "multiple_tools", "no_indexes", "null_indexes", "null_resource", "null_tools", "no_tools"):
        source = copy.deepcopy(SOURCE)
        if change == "multiple_indexes":
            source["tools"][1]["azure_ai_search"]["indexes"] *= 2
        elif change == "multiple_tools":
            source["tools"].append(copy.deepcopy(source["tools"][1]))
        elif change == "no_indexes":
            source["tools"][1]["azure_ai_search"]["indexes"] = []
        elif change == "null_indexes":
            source["tools"][1]["azure_ai_search"]["indexes"] = None
        elif change == "null_resource":
            source["tools"][1]["azure_ai_search"] = None
        elif change == "null_tools":
            source["tools"] = None
        else:
            source["tools"] = []
        project = Project(source=source)
        with raises(ValueError, "exactly one Azure AI Search"):
            agent.clone_agent(project, settings, "production-agent")
        assert not project.agents.created
    print("PASS existing differing targets, source-as-target, and ambiguous/missing Search resources are rejected")

    def corrupt_readback(agents):
        agents.versions["evaluation-agent"].definition.as_dict()["instructions"] = "unexpected"

    with raises(RuntimeError, "readback differs"):
        agent.clone_agent(Project(on_create=corrupt_readback), settings, "production-agent")

    for field in ("version", "definition"):
        def change_source(agents):
            source = agents.versions["production-agent"]
            if field == "version":
                source.version = "2"
            else:
                source.definition.as_dict()["instructions"] = "Concurrent source edit"

        with raises(RuntimeError, "Source agent changed"):
            agent.clone_agent(Project(on_create=change_source), settings, "production-agent")
    error = HttpResponseError("Access denied to target")
    project = Project()
    project.agents.get_errors["evaluation-agent"] = error
    try:
        agent.clone_agent(project, settings, "production-agent")
    except HttpResponseError as exc:
        assert exc is error
    else:
        raise AssertionError("Target API error was swallowed")
    assert not project.agents.created
    print("PASS definition readback, source-version/definition checks, and original API errors")

    for failure in (None, HttpResponseError("Token tenant old does not match resource tenant"), HttpResponseError("service unavailable")):
        project = Project()
        credential = MagicMock()
        stderr = StringIO()
        with patch.object(agent, "load_settings", return_value=settings), patch.object(agent, "_credential", return_value=credential), patch.object(agent, "AIProjectClient", return_value=project), redirect_stderr(stderr), redirect_stdout(StringIO()):
            if failure is None:
                assert agent.main(["--clone-from", "production-agent"]) == 0
            else:
                with patch.object(agent, "clone_agent", side_effect=failure):
                    if "tenant" in str(failure):
                        assert agent.main(["--clone-from", "production-agent"]) == 1
                        assert "Authenticated against the wrong tenant" in stderr.getvalue()
                        assert "az login --tenant" in stderr.getvalue()
                    else:
                        with raises(HttpResponseError, "service unavailable"):
                            agent.main(["--clone-from", "production-agent"])
        assert project.closed
        credential.__exit__.assert_called_once()
    print("PASS CLI closes clients/credentials on success and API failure, retaining wrong-tenant guidance")
    for web_enabled in (True, False):
        project = Project()
        credential = MagicMock()
        with patch.object(agent, "load_settings", return_value=settings), patch.object(agent, "_credential", return_value=credential), patch.object(agent, "AIProjectClient", return_value=project), patch.object(agent, "create_agent", return_value=(object(), web_enabled)), redirect_stdout(StringIO()):
            assert agent.main([]) == (0 if web_enabled else agent.EXIT_DEGRADED)
        assert project.closed
        credential.__exit__.assert_called_once()
    print("PASS normal publication retains the optional-Bing degraded exit status")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
