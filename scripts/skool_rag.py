#!/usr/bin/env python3
"""Local Skool snapshot ingestion, retrieval, and buyer-signal scoring.

This is intentionally dependency-free. It indexes exported Markdown/text
snapshots from data/raw/skool_snapshots into JSONL plus a compact TF-IDF index.
It is not a browser scraper and does not claim private/collapsed content.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "skool_snapshots"
OUT_DIR = ROOT / "data" / "processed" / "skool_rag"
DOCS_PATH = OUT_DIR / "documents.jsonl"
INDEX_PATH = OUT_DIR / "tfidf_index.json"
SIGNALS_PATH = OUT_DIR / "buyer_signals.jsonl"

TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_'-]{1,}")
HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)

BUYER_KEYWORDS = {
    "hiring": 4,
    "paid": 4,
    "project": 3,
    "client": 3,
    "retainer": 5,
    "manychat": 5,
    "meta": 3,
    "leads": 4,
    "calls": 4,
    "booked": 3,
    "workflow": 3,
    "automation": 2,
    "n8n": 3,
    "ghl": 3,
    "crm": 3,
    "audit": 3,
    "diagnostic": 4,
    "build": 2,
    "replace": 3,
    "scale": 3,
    "revenue": 4,
}

RISK_KEYWORDS = {
    "guarantee": 3,
    "passive income": 3,
    "email": 2,
    "gmail": 3,
    "scrape": 3,
    "private": 3,
}


@dataclasses.dataclass
class Document:
    doc_id: str
    source_path: str
    title: str
    section: str
    text: str
    tags: list[str]
    confidence: str


def tokenize(text: str) -> list[str]:
    return [m.group(0).lower().strip("'-") for m in TOKEN_RE.finditer(text)]


def stable_id(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8", errors="ignore"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def extract_tags(text: str) -> list[str]:
    tags: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- `") and line.endswith("`"):
            tags.append(line[3:-1])
    return tags


def confidence_from_text(text: str) -> str:
    lowered = text.lower()
    if "visible excerpt only" in lowered or "not fully expanded" in lowered:
        return "medium_visible_excerpt"
    if "captured through logged-in safari" in lowered:
        return "medium_authenticated_snapshot"
    return "low_untyped_snapshot"


def split_markdown(path: Path) -> list[Document]:
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        return []

    matches = list(HEADING_RE.finditer(raw))
    title = path.stem
    if matches and matches[0].start() == 0:
        title = matches[0].group(2).strip()

    tags = extract_tags(raw)
    confidence = confidence_from_text(raw)
    docs: list[Document] = []

    if len(raw) < 2400:
        docs.append(
            Document(
                doc_id=stable_id(str(path.relative_to(ROOT)), "full", raw),
                source_path=str(path.relative_to(ROOT)),
                title=title,
                section="full",
                text=raw,
                tags=tags,
                confidence=confidence,
            )
        )
        return docs

    chunks: list[tuple[str, str]] = []
    if matches:
        for idx, match in enumerate(matches):
            start = match.start()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw)
            section = match.group(2).strip()
            chunks.append((section, raw[start:end].strip()))
    else:
        paragraphs = raw.split("\n\n")
        buf: list[str] = []
        for para in paragraphs:
            if sum(len(x) for x in buf) + len(para) > 1800 and buf:
                chunks.append(("chunk", "\n\n".join(buf)))
                buf = []
            buf.append(para)
        if buf:
            chunks.append(("chunk", "\n\n".join(buf)))

    for i, (section, text) in enumerate(chunks):
        if len(text) < 80:
            continue
        docs.append(
            Document(
                doc_id=stable_id(str(path.relative_to(ROOT)), section, text),
                source_path=str(path.relative_to(ROOT)),
                title=title,
                section=section if section != "chunk" else f"chunk-{i + 1}",
                text=text,
                tags=tags,
                confidence=confidence,
            )
        )
    return docs


def iter_snapshots() -> Iterable[Path]:
    yield from sorted(RAW_DIR.glob("*.md"))
    yield from sorted(RAW_DIR.glob("*.txt"))


def build_index(docs: list[Document]) -> dict:
    doc_tokens = {doc.doc_id: tokenize(doc.text) for doc in docs}
    df: collections.Counter[str] = collections.Counter()
    for tokens in doc_tokens.values():
        df.update(set(tokens))

    n_docs = max(1, len(docs))
    vectors: dict[str, dict[str, float]] = {}
    for doc in docs:
        tf = collections.Counter(doc_tokens[doc.doc_id])
        total = max(1, sum(tf.values()))
        vec: dict[str, float] = {}
        for term, count in tf.items():
            if len(term) < 3:
                continue
            idf = math.log((1 + n_docs) / (1 + df[term])) + 1
            vec[term] = (count / total) * idf
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vectors[doc.doc_id] = {term: val / norm for term, val in vec.items()}

    return {
        "doc_count": len(docs),
        "df": dict(df),
        "vectors": vectors,
    }


def score_buyer_signal(doc: Document) -> dict:
    lowered = doc.text.lower()
    score = 0
    reasons: list[str] = []
    for keyword, weight in BUYER_KEYWORDS.items():
        if keyword in lowered:
            score += weight
            reasons.append(keyword)
    for keyword, weight in RISK_KEYWORDS.items():
        if keyword in lowered:
            score -= weight
            reasons.append(f"risk:{keyword}")

    if "checkout" in lowered or "dm me" in lowered:
        score += 2
        reasons.append("direct-response")
    if "$" in doc.text or "£" in doc.text:
        score += 2
        reasons.append("money-mentioned")
    if any(tag.startswith("buyer_signal:strong") for tag in doc.tags):
        score += 5
        reasons.append("tag:buyer_signal:strong")

    return {
        "doc_id": doc.doc_id,
        "score": score,
        "title": doc.title,
        "section": doc.section,
        "source_path": doc.source_path,
        "confidence": doc.confidence,
        "reasons": sorted(set(reasons)),
        "summary": summarize(doc.text),
    }


def summarize(text: str, limit: int = 360) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return clean[: limit - 1] + "…" if len(clean) > limit else clean


def load_docs() -> list[Document]:
    docs: list[Document] = []
    if not DOCS_PATH.exists():
        return docs
    for line in DOCS_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            docs.append(Document(**json.loads(line)))
    return docs


def ingest(_: argparse.Namespace) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    docs: list[Document] = []
    for path in iter_snapshots():
        docs.extend(split_markdown(path))

    DOCS_PATH.write_text(
        "\n".join(json.dumps(dataclasses.asdict(doc), ensure_ascii=False) for doc in docs) + "\n",
        encoding="utf-8",
    )
    INDEX_PATH.write_text(json.dumps(build_index(docs), indent=2, sort_keys=True), encoding="utf-8")

    signals = sorted((score_buyer_signal(doc) for doc in docs), key=lambda item: item["score"], reverse=True)
    SIGNALS_PATH.write_text(
        "\n".join(json.dumps(signal, ensure_ascii=False) for signal in signals) + "\n",
        encoding="utf-8",
    )

    print(f"Indexed {len(docs)} documents from {RAW_DIR.relative_to(ROOT)}")
    print(f"Wrote {DOCS_PATH.relative_to(ROOT)}")
    print(f"Wrote {INDEX_PATH.relative_to(ROOT)}")
    print(f"Wrote {SIGNALS_PATH.relative_to(ROOT)}")


def query(args: argparse.Namespace) -> None:
    docs = {doc.doc_id: doc for doc in load_docs()}
    if not docs or not INDEX_PATH.exists():
        raise SystemExit("Index missing. Run: scripts/skool_rag.py ingest")
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    q_tf = collections.Counter(tokenize(args.query))
    q_vec: dict[str, float] = {}
    n_docs = max(1, index["doc_count"])
    for term, count in q_tf.items():
        df = index["df"].get(term, 0)
        idf = math.log((1 + n_docs) / (1 + df)) + 1
        q_vec[term] = count * idf
    norm = math.sqrt(sum(v * v for v in q_vec.values())) or 1.0
    q_vec = {term: val / norm for term, val in q_vec.items()}

    scored: list[tuple[float, Document]] = []
    for doc_id, vec in index["vectors"].items():
        score = sum(q_vec.get(term, 0.0) * vec.get(term, 0.0) for term in q_vec)
        if score > 0 and doc_id in docs:
            scored.append((score, docs[doc_id]))
    scored.sort(reverse=True, key=lambda item: item[0])

    for score, doc in scored[: args.top_k]:
        print(f"[{score:.3f}] {doc.title} / {doc.section}")
        print(f"source={doc.source_path} confidence={doc.confidence}")
        print(summarize(doc.text, 520))
        print()


def signals(args: argparse.Namespace) -> None:
    if not SIGNALS_PATH.exists():
        raise SystemExit("Signals missing. Run: scripts/skool_rag.py ingest")
    rows = [json.loads(line) for line in SIGNALS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows[: args.top_k]:
        print(f"[{row['score']}] {row['title']} / {row['section']}")
        print(f"source={row['source_path']} confidence={row['confidence']}")
        print(f"reasons={', '.join(row['reasons'])}")
        print(row["summary"])
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest")
    p_ingest.set_defaults(func=ingest)

    p_query = sub.add_parser("ask")
    p_query.add_argument("query")
    p_query.add_argument("--top-k", type=int, default=5)
    p_query.set_defaults(func=query)

    p_signals = sub.add_parser("signals")
    p_signals.add_argument("--top-k", type=int, default=10)
    p_signals.set_defaults(func=signals)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
