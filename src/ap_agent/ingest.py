"""Build the policy knowledge base: parse, chunk, embed, validate, activate.

Every document is ingested, including superseded, irrelevant and adversarial ones;
retrieval labels them rather than hiding them. A build runs in one transaction, so a
build that fails validation never replaces the live index.
"""

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

import psycopg
import yaml
from pydantic import BaseModel, ValidationError

from ap_agent.config import Settings
from ap_agent.db import connect
from ap_agent.embeddings import Embedder
from ap_agent.retrieval import search, to_pgvector
from ap_agent.rules_config import RulesConfig

DocType = Literal["policy", "supplier_document", "reference_extract"]
GOLDEN_TOP_K = 5


class IngestError(Exception):
    """The corpus or the new index failed validation; the live index is unchanged."""


class FrontMatter(BaseModel):
    document_id: str
    title: str
    version: str
    effective_date: date
    status: Literal["current", "superseded", "untrusted"]
    classification: str


@dataclass(frozen=True)
class Document:
    meta: FrontMatter
    body: str
    content_hash: str

    @property
    def doc_type(self) -> DocType:
        if self.meta.status != "untrusted":
            return "policy"
        if self.meta.classification == "external-unverified":
            return "supplier_document"
        return "reference_extract"


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc: Document
    section: str  # "§2 Tolerances", or "body" for a document without sections
    citation: str
    text: str

    @property
    def embed_text(self) -> str:
        """The section with a context header, so the vector knows where the text came from."""
        m = self.doc.meta
        return (
            f"[{m.document_id} v{m.version} · {m.title} · {self.section} · {m.status}]\n{self.text}"
        )


class GoldenQuery(BaseModel):
    query: str
    expect: str | None  # document that must be in the top policy results; None: no results


@dataclass(frozen=True)
class IngestReport:
    index_version: str
    documents: int
    chunks: int
    golden: list[tuple[GoldenQuery, int | None]]  # each query with its document's rank


def run_ingest(settings: Settings, embedder: Embedder, rules: RulesConfig) -> IngestReport:
    docs = load_corpus(settings.corpus_source)
    validate_corpus(docs, rules)
    golden = load_golden(settings.golden_queries)
    with connect("ap_ingest", settings) as conn:
        return build_index(conn, docs, embedder, golden, settings.retrieval_min_score, date.today())


def load_corpus(folder: Path) -> list[Document]:
    docs = [parse_document(path) for path in sorted(folder.glob("*.md"))]
    if not docs:
        raise IngestError(f"no documents in {folder}")
    return docs


def parse_document(path: Path) -> Document:
    raw = path.read_text()
    match = re.match(r"---\n(.*?)\n---\n(.*)", raw, re.DOTALL)
    if match is None:
        raise IngestError(f"{path.name}: no front-matter")
    try:
        # BaseLoader keeps every value a string, so version 2.10 does not become 2.1.
        meta = FrontMatter.model_validate(yaml.load(match[1], Loader=yaml.BaseLoader))
    except ValidationError as e:
        raise IngestError(f"{path.name}: invalid front-matter\n{e}") from e
    return Document(meta=meta, body=match[2].strip(), content_hash=_sha256(raw))


def chunk_document(doc: Document) -> list[Chunk]:
    """One chunk per `##` section; a document without sections is one `body` chunk."""
    body = re.sub(r"\A# .*\n", "", doc.body).strip()  # the title is in the context header
    parts = re.split(r"^## +(.+?)\s*$", body, flags=re.MULTILINE)
    doc_id = doc.meta.document_id
    if len(parts) == 1:
        return [Chunk(f"{doc_id}#body", doc, "body", _citation(doc, None), body)]
    chunks = []
    for heading, text in zip(parts[1::2], parts[2::2], strict=True):
        number, name = re.match(r"(?:(\d+)\.\s*)?(.+)", heading).groups()
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        section = f"§{number} {name}" if number else name
        chunks.append(Chunk(f"{doc_id}#{slug}", doc, section, _citation(doc, number), text.strip()))
    return chunks


def _citation(doc: Document, section_number: str | None) -> str:
    m = doc.meta
    ref = f"{m.document_id} v{m.version}" + (f" §{section_number}" if section_number else "")
    return ref if m.status == "current" else f"{ref} ({m.status.upper()})"


def validate_corpus(docs: list[Document], rules: RulesConfig) -> None:
    """One current version per document, and the rule values match the current policies."""
    current: dict[str, str] = {}
    for doc in docs:
        if doc.meta.status == "current":
            if doc.meta.document_id in current:
                raise IngestError(f"more than one current version of {doc.meta.document_id}")
            current[doc.meta.document_id] = doc.meta.version
    for doc_id, version in rules.sources.items():
        if current.get(doc_id) != version:
            found = f"v{current[doc_id]}" if doc_id in current else "no current version"
            raise IngestError(
                f"rules_config.yaml was copied from {doc_id} v{version} but the corpus has "
                f"{found}; update the rule values and their source version first"
            )


def load_golden(path: Path) -> list[GoldenQuery]:
    return [GoldenQuery.model_validate(item) for item in yaml.safe_load(path.read_text())]


def build_index(
    conn: psycopg.Connection,
    docs: list[Document],
    embedder: Embedder,
    golden: list[GoldenQuery],
    min_score: float,
    today: date,
) -> IngestReport:
    chunks = [chunk for doc in docs for chunk in chunk_document(doc)]
    vectors = embedder.embed_documents([chunk.embed_text for chunk in chunks])
    if any(len(v) != embedder.dim for v in vectors):
        raise IngestError(f"{embedder.model_id} returned vectors that are not {embedder.dim}-d")
    index_version = f"kb-{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"

    with conn.transaction():
        conn.execute(
            """insert into agent.kb_index_versions (index_version, embed_model, embed_dim, status)
               values (%s, %s, %s, 'BUILDING')""",
            [index_version, embedder.model_id, embedder.dim],
        )
        with conn.cursor() as cur:
            cur.executemany(
                """insert into agent.kb_documents (index_version, doc_id, version, title, doc_type,
                     status, effective_date, classification, content_hash)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [
                    (
                        index_version,
                        d.meta.document_id,
                        d.meta.version,
                        d.meta.title,
                        d.doc_type,
                        d.meta.status,
                        d.meta.effective_date,
                        d.meta.classification,
                        d.content_hash,
                    )
                    for d in docs
                ],
            )
            cur.executemany(
                """insert into agent.kb_chunks (index_version, chunk_id, doc_id, doc_version,
                     section, citation, text, content_hash, embedding)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector)""",
                [
                    (
                        index_version,
                        c.chunk_id,
                        c.doc.meta.document_id,
                        c.doc.meta.version,
                        c.section,
                        c.citation,
                        c.text,
                        _sha256(c.embed_text),
                        to_pgvector(v),
                    )
                    for c, v in zip(chunks, vectors, strict=True)
                ],
            )

        results = []
        for gq in golden:
            found = search(conn, embedder, index_version, gq.query, GOLDEN_TOP_K, min_score, today)
            if gq.expect is None:
                if found.policy:
                    raise IngestError(
                        f"off-topic golden query {gq.query!r} matched {found.policy[0].citation} "
                        f"({found.policy[0].score}); RETRIEVAL_MIN_SCORE may be too low"
                    )
                results.append((gq, None))
                continue
            ranks = [i for i, hit in enumerate(found.policy, 1) if hit.doc_id == gq.expect]
            if not ranks:
                raise IngestError(f"golden query missed {gq.expect}: {gq.query!r}")
            results.append((gq, ranks[0]))

        conn.execute(
            "update agent.kb_index_versions set status = 'RETIRED' where status = 'ACTIVE'"
        )
        conn.execute(
            """update agent.kb_index_versions set status = 'ACTIVE', activated_at = now()
               where index_version = %s""",
            [index_version],
        )
    return IngestReport(index_version, len(docs), len(chunks), results)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
