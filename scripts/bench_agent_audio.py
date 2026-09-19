"""Measure text-submission to received audio on the Voice Live AGENT path.

This is NOT microphone/VAD-to-playback latency and does not measure the avatar
video path. Uses disposable agents with frozen retrieval; production is untouched.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from azure.ai.projects import AIProjectClient
from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    AzureStandardVoice, InputTextContentPart, Modality, OutputAudioFormat,
    RequestSession, SystemMessageItem, UserMessageItem,
)
from azure.identity import AzureCliCredential
from azure.identity.aio import AzureCliCredential as AsyncAzureCliCredential
from dotenv import dotenv_values

from bench_routing_matrix import benchmark_definition
from smoke_foundry_agent import _fetch_catalog


def event_type(event) -> str:
    value = event.type
    return str(getattr(value, "value", value)).lower()


async def audio_turn(endpoint, project_name, agent_name, version, catalog, question, credential, voice, api_version):
    connection_start = time.perf_counter()
    async with asyncio.timeout(150):
        async with connect(
            endpoint=endpoint, credential=credential, api_version=api_version,
            agent_name=agent_name, agent_version=version, project_name=project_name,
        ) as conn:
            await conn.session.update(session=RequestSession(
                modalities=[Modality.TEXT, Modality.AUDIO],
                voice=AzureStandardVoice(name=voice),
                output_audio_format=OutputAudioFormat.PCM16,
                turn_detection=None,
            ))
            async with asyncio.timeout(30):
                async for event in conn:
                    kind = event_type(event)
                    if kind == "error":
                        raise RuntimeError(f"Voice Live session rejected: {event.as_dict()}")
                    if kind == "session.updated":
                        break
                else:
                    raise RuntimeError("Voice Live closed before session acknowledgement")
            setup_s = time.perf_counter() - connection_start
            await conn.conversation.item.create(item=SystemMessageItem(content=[InputTextContentPart(text=catalog)]))
            started = time.perf_counter()
            await conn.conversation.item.create(item=UserMessageItem(content=[InputTextContentPart(text=question)]))
            await conn.response.create()
            first_audio = first_transcript = None
            audio_bytes = 0
            transcript = []
            types = set()
            async for event in conn:
                kind = event_type(event)
                types.add(kind)
                elapsed = time.perf_counter() - started
                if kind == "error":
                    raise RuntimeError(f"Voice Live turn rejected: {event.as_dict()}")
                if kind in ("response.audio.delta", "response.output_audio.delta"):
                    data = event.delta if isinstance(event.delta, bytes) else base64.b64decode(event.delta, validate=True)
                    if data and first_audio is None:
                        first_audio = elapsed
                    audio_bytes += len(data)
                elif kind in ("response.audio_transcript.delta", "response.output_audio_transcript.delta"):
                    if event.delta and first_transcript is None:
                        first_transcript = elapsed
                    transcript.append(event.delta)
                elif kind == "response.done":
                    response = event.response.as_dict()
                    if response.get("status") != "completed":
                        raise RuntimeError(f"Voice Live response did not complete: {response}")
                    if not audio_bytes:
                        raise RuntimeError("Completed response had no received audio")
                    return {
                        "status": "completed", "connection_setup_s": setup_s,
                        "text_submit_to_first_received_audio_s": first_audio,
                        "text_submit_to_first_audio_transcript_s": first_transcript,
                        "completion_s": elapsed, "audio_bytes": audio_bytes,
                        "transcript": "".join(transcript), "response": response,
                        "observed_event_types": sorted(types),
                        "playback_latency_measured": False, "speech_input_used": False,
                    }
            raise RuntimeError("Voice Live closed without response.done")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--case-ids", nargs="+", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", choices=("none", "low"), required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--api-version", default="2026-04-10")
    parser.add_argument("--voice", default="en-US-AvaMultilingualNeural")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("runs must be positive")
    output = args.output_dir.resolve()
    repo = Path(__file__).resolve().parent.parent
    if output.is_relative_to(repo):
        parser.error("Private transcripts must stay outside the repository")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        parser.error("Output directory must be empty")
    cases = [case for case in json.loads(args.cases.read_text(encoding="utf-8")) if case["id"] in args.case_ids]
    if {case["id"] for case in cases} != set(args.case_ids):
        parser.error("One or more case IDs are missing")
    cfg = dotenv_values(args.env_file)
    for key in ("AZURE_SEARCH_ENDPOINT", "SEARCH_INDEX_NAME"):
        os.environ[key] = cfg[key]
    parsed = urlsplit(cfg["PROJECT_ENDPOINT"])
    project_name = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    endpoint = f"{parsed.scheme}://{parsed.netloc}"
    with AzureCliCredential(process_timeout=120) as credential, AIProjectClient(
        endpoint=cfg["PROJECT_ENDPOINT"], credential=credential,
    ) as project:
        source = project.agents.get(cfg["AGENT_NAME"])
        source_definition = source.versions.latest.definition.as_dict()
        definition = benchmark_definition(source_definition, args.model, args.effort, None)
        indexes = [index for tool in definition["tools"] if tool["type"] == "azure_ai_search" for index in tool["azure_ai_search"]["indexes"]]
        if len(indexes) != 1 or indexes[0]["query_type"] != "semantic" or indexes[0]["top_k"] != 5:
            raise RuntimeError("Expected the frozen semantic/top-5 agent")
        catalog = _fetch_catalog(credential=credential)
        if not catalog:
            raise RuntimeError("Catalogue unavailable")
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
        catalog = f"[SILENT REFERENCE DATA]\nTODAY: {today} (UTC).\n\n{catalog}"
        name = f"bench-audio-{uuid.uuid4().hex[:12]}"
        created = project.agents.create_version(name, body={"definition": definition})
        metadata = {
            "model": args.model, "effort": args.effort, "agent": name, "version": created.version,
            "source_agent": cfg["AGENT_NAME"], "source_version": source.versions.latest.version,
            "definition": definition, "voice": args.voice, "api_version": args.api_version,
            "measurement": "text submission to first received PCM audio; NOT speech-end to playback",
        }
        (output / "manifest.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        async def run():
            async with AsyncAzureCliCredential(process_timeout=120) as cred:
                with (output / "turns.jsonl").open("w", encoding="utf-8", buffering=1) as log:
                    for number in range(1, args.runs + 1):
                        for case in cases:
                            result = {"run": number, "case_id": case["id"], "model": args.model, "effort": args.effort}
                            try:
                                result.update(await audio_turn(
                                    endpoint, project_name, name, created.version, catalog, case["question"],
                                    cred, args.voice, args.api_version,
                                ))
                            except (RuntimeError, TimeoutError, ValueError) as error:
                                result.update(status="error", error_type=type(error).__name__, error=str(error))
                                log.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
                                raise
                            log.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
                            print(f"audio run={number} case={case['id']} first_received_s={result['text_submit_to_first_received_audio_s']:.3f}", flush=True)
                            await asyncio.sleep(3)

        try:
            actual = project.agents.get_version(name, created.version).definition.as_dict()
            if actual != definition:
                raise RuntimeError("Audio agent definition readback differs")
            asyncio.run(run())
        finally:
            project.agents.delete(name)
            (output / "cleanup.json").write_text(json.dumps({"deleted": name}), encoding="utf-8")
        latest = project.agents.get(cfg["AGENT_NAME"]).versions.latest
        if latest.version != source.versions.latest.version or latest.definition.as_dict() != source_definition:
            raise RuntimeError("Source agent changed during audio run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
