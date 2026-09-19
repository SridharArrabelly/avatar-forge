"""Offline scoring checks using synthetic source text, never the private corpus."""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from retrieval_evaluation import build_cases, covered_length, layout_chunks, locate_chunk, native_chunk_map, query_for, read_original, score


def main() -> int:
    assert covered_length(10, 30, [(10, 20), (15, 25), (25, 30)]) == 20
    assert covered_length(10, 30, [(10, 19), (20, 30)]) == 19
    assert covered_length(10, 30, [(0, 12), (28, 40)]) == 4
    assert covered_length(10, 30, []) == 0
    case = {"source": "meeting-a", "evidence": [{"paragraph": 1, "start": 10, "end": 30, "text": "synthetic"}]}
    chunks = {
        "a1": {"source": "meeting-a", "start": 0, "end": 20},
        "a2": {"source": "meeting-a", "start": 18, "end": 40},
        "unrelated-section": {"source": "meeting-a", "start": 50, "end": 80},
        "wrong-meeting": {"source": "meeting-b", "start": 10, "end": 30},
    }
    assert score(case, [{"id": "a1"}, {"id": "a2"}], chunks)["complete_case"]
    assert not score(case, [{"id": "a1"}], chunks)["complete_case"]
    assert not score(case, [{"id": "unrelated-section"}], chunks)["complete_case"]
    wrong = score(case, [{"id": "wrong-meeting"}], chunks)
    assert wrong["wrong_source_hits"] == 1 and wrong["evidence_char_coverage"] == 0
    assert score(case, [], chunks)["evidence_char_coverage"] == 0
    assert score(case, [{"id": "a1"}, {"id": "a1"}], chunks)["evidence_char_coverage"] == 0.5
    try:
        score(case, [{"id": "new-unverified-chunk"}], chunks)
    except KeyError:
        pass
    else:
        raise AssertionError("Unknown chunks cannot silently pass")
    print("PASS evidence scoring requires all original spans, not a document hit or duplicate chunks")

    document = {"source": "synthetic", "text": "The board approved a test. Another sentence."}
    assert locate_chunk("[Document: synthetic]\n\nThe board approved a test.", document) == (0, 26)
    try:
        locate_chunk("[Document: synthetic]\n\nAn invented sentence.", document)
    except ValueError:
        pass
    else:
        raise AssertionError("Invented chunk text accepted")
    print("PASS chunks must map back to exact original text")
    audit = {"known-id": {"source": "synthetic"}}
    partial = [{"id": "known-id", "content": "Title\n[Document: synthetic]\n\nThe board approved"}]
    delivered = native_chunk_map(partial, audit, {"synthetic": document})
    required = {"source": "synthetic", "evidence": [{"paragraph": 0, "start": 0, "end": 26}]}
    assert not score(required, partial, delivered)["complete_case"]
    print("PASS a correct document ID cannot hide truncated native tool evidence")

    xml = """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
      <w:p><w:r><w:t>Board Meeting 15 March 2006</w:t></w:r></w:p>
      <w:p><w:r><w:t>Attendees: Alice; Bob.</w:t></w:r></w:p>
      <w:p><w:r><w:t>Key Discussion Points:</w:t></w:r></w:p>
      <w:p><w:r><w:t>Discussed the synthetic test.</w:t></w:r></w:p>
      <w:p><w:r><w:t>Decisions Made:</w:t></w:r></w:p>
      <w:p><w:r><w:t>Approved a test, not a launch.</w:t></w:r></w:p>
      <w:p><w:r><w:t>Action Items:</w:t></w:r></w:p>
      <w:p><w:r><w:t>Alice:</w:t><w:br/><w:t>check the test.</w:t></w:r></w:p>
      <w:p><w:r><w:t>Next Steps:</w:t></w:r></w:p>
      <w:p><w:r><w:t>Review later.</w:t></w:r></w:p>
    </w:body></w:document>"""
    with TemporaryDirectory() as temp:
        path = Path(temp) / "Board Meeting 15 March 2006.docx"
        with ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", xml)
        original = read_original(path)
        cases = build_cases([original])
    assert len(cases) == 4
    assert all(item["split"] == "development" for item in cases)
    for item in cases:
        for evidence in item["evidence"]:
            assert original["text"][evidence["start"]:evidence["end"]] == evidence["text"]
    assert cases[0]["evidence"][0]["text"] == "Alice; Bob."
    actions = next(item for item in cases if item["intent"] == "actions")
    assert actions["evidence"][0]["text"] == "Alice: check the test."
    assert query_for(actions, "canonical") == actions["query"]
    assert query_for(actions, "natural") == "Which follow-up tasks were assigned and who owns them from the 15 March 2006 board meeting?"
    print("PASS independent XML extraction and source evidence offsets agree")
    for layout in ("paragraph", "section"):
        chunks = layout_chunks([original], layout)
        chunk_map = {}
        for chunk in chunks:
            start, end = locate_chunk(chunk["content"], original)
            chunk_map[chunk["id"]] = {"source": original["source"], "start": start, "end": end}
        for case in cases:
            assert score(case, [{"id": chunk["id"]} for chunk in chunks], chunk_map)["complete_case"]
        assert chunks[0]["chunk_index"] == 0
    print("PASS both evaluation layouts preserve every required original evidence unit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
