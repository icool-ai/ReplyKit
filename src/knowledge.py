"""Knowledge base ingestion and vector store management."""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import RLock

from langchain_community.document_loaders import TextLoader
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from qdrant_client import QdrantClient
from qdrant_client.http.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PointStruct,
    VectorParams,
)

from src.config import Settings
from src.docx_images import load_docx_with_images
from src.faq_loader import faq_entry_to_documents
from src.faq_store import FaqRow, FaqStore

CONTENT_KEY = "page_content"
SUPPORTED_SUFFIXES = {".txt", ".md", ".docx"}
FAQ_DB_SOURCE = "faq_db"
_EMBED_BATCH_SIZE = 64
_EMBED_MAX_WORKERS = 4

_logger = logging.getLogger(__name__)

_client: QdrantClient | None = None
_client_path: str | None = None
# Serialize local Qdrant access (path mode is not safely concurrent).
_vector_lock = RLock()


def _get_embeddings(settings: Settings) -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        model=settings.embedding_model,
        openai_api_key=settings.dashscope_api_key,
        openai_api_base=settings.openai_api_base,
        check_embedding_ctx_length=False,
    )


def _get_qdrant_client(settings: Settings) -> QdrantClient:
    """Reuse one local Qdrant client to avoid Windows file-lock conflicts."""
    global _client, _client_path

    settings.qdrant_path.mkdir(parents=True, exist_ok=True)
    path = str(settings.qdrant_path.resolve())
    if _client is not None and _client_path == path:
        return _client

    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
        _client = None
        _client_path = None

    _client = QdrantClient(path=path)
    _client_path = path
    return _client


def _collection_exists(client: QdrantClient, name: str) -> bool:
    collections = [c.name for c in client.get_collections().collections]
    return name in collections


def _infer_doc_type(path: Path, settings: Settings) -> str:
    try:
        resolved = path.resolve()
        docs_dir = settings.docs_dir.resolve()
        faq_dir = settings.faq_dir.resolve()
        if resolved == docs_dir or docs_dir in resolved.parents:
            return "manual"
        if resolved == faq_dir or faq_dir in resolved.parents:
            return "faq"
    except OSError:
        pass
    if path.suffix.lower() == ".docx":
        return "manual"
    return "faq"


def _load_docx(path: Path, settings: Settings) -> list[Document]:
    settings.assets_dir.mkdir(parents=True, exist_ok=True)
    return load_docx_with_images(path, settings.assets_dir)


def _is_temp_office_file(path: Path) -> bool:
    """Skip Word lock files like '~$操作说明.docx' created while the doc is open."""
    name = path.name
    return name.startswith("~$") or name.startswith(".~")


def _load_file(path: Path, settings: Settings) -> list[Document]:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        return []
    if _is_temp_office_file(path):
        return []

    resolved = path.resolve()
    if suffix == ".docx":
        docs = _load_docx(resolved, settings)
    else:
        docs = TextLoader(str(resolved), encoding="utf-8").load()

    doc_type = _infer_doc_type(resolved, settings)
    for doc in docs:
        metadata = dict(doc.metadata or {})
        metadata["source"] = str(resolved)
        metadata["doc_type"] = doc_type
        if "images" not in metadata:
            metadata["images"] = []
        doc.metadata = metadata
    return docs


def _faq_store(settings: Settings) -> FaqStore:
    return FaqStore(settings.faq_db_path)


def _faq_row_to_documents(row: FaqRow, settings: Settings) -> list[Document]:
    return faq_entry_to_documents(
        faq_id=row.id,
        question=row.question,
        answer=row.answer,
        similar=row.similar,
        category=row.category,
        source=FAQ_DB_SOURCE,
        max_similar=settings.faq_max_similar,
    )


def _load_faq_documents(settings: Settings) -> list[Document]:
    store = _faq_store(settings)
    documents: list[Document] = []
    for row in store.list_enabled():
        documents.extend(_faq_row_to_documents(row, settings))
    return documents


def _iter_knowledge_files(settings: Settings) -> list[Path]:
    """Disk files that participate in rebuild detection (manuals only).

    FAQ source of truth is SQLite; faq.json is import-only.
    """
    files: list[Path] = []
    if settings.docs_dir.exists():
        for path in sorted(settings.docs_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            if _is_temp_office_file(path):
                continue
            files.append(path.resolve())
    return files


def _load_documents(source_dir: Path, settings: Settings) -> list[Document]:
    if not source_dir.exists():
        return []

    docs: list[Document] = []
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file():
            continue
        if "legacy" in path.parts:
            continue
        if _is_temp_office_file(path):
            continue
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        docs.extend(_load_file(path, settings))
    return docs


def _load_all_documents(settings: Settings) -> list[Document]:
    documents: list[Document] = []
    documents.extend(_load_faq_documents(settings))
    documents.extend(_load_documents(settings.docs_dir, settings))
    return documents


def _split_documents(documents: list, settings: Settings) -> list:
    if not documents:
        return []

    # Structured FAQ entries stay as one vector each (do not chunk).
    faq_docs = [d for d in documents if d.metadata.get("doc_type") == "faq"]
    other_docs = [d for d in documents if d.metadata.get("doc_type") != "faq"]

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        separators=["\n\n", "\n", "。", "！", "？", " ", ""],
    )
    return faq_docs + splitter.split_documents(other_docs)


def vectorstore_exists(settings: Settings) -> bool:
    client = _get_qdrant_client(settings)
    return _collection_exists(client, settings.collection_name)


def _indexed_source_names(settings: Settings) -> set[str]:
    client = _get_qdrant_client(settings)
    if not _collection_exists(client, settings.collection_name):
        return set()

    names: set[str] = set()
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=settings.collection_name,
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            source = (point.payload or {}).get("source")
            if source:
                names.add(Path(str(source)).name)
        if offset is None:
            break
    return names


def _any_chunk_has_images(settings: Settings) -> bool:
    client = _get_qdrant_client(settings)
    if not _collection_exists(client, settings.collection_name):
        return False
    points, _ = client.scroll(
        collection_name=settings.collection_name,
        limit=50,
        with_payload=True,
        with_vectors=False,
    )
    for point in points:
        images = (point.payload or {}).get("images")
        if isinstance(images, list) and images:
            return True
        if isinstance(images, str) and images.strip():
            return True
    return False


def _any_chunk_has_faq_id(settings: Settings) -> bool:
    client = _get_qdrant_client(settings)
    if not _collection_exists(client, settings.collection_name):
        return False
    points, _ = client.scroll(
        collection_name=settings.collection_name,
        limit=50,
        with_payload=True,
        with_vectors=False,
    )
    for point in points:
        if (point.payload or {}).get("faq_id"):
            return True
    return False


def _any_chunk_has_per_phrase_faq(settings: Settings) -> bool:
    client = _get_qdrant_client(settings)
    if not _collection_exists(client, settings.collection_name):
        return False
    points, _ = client.scroll(
        collection_name=settings.collection_name,
        limit=80,
        with_payload=True,
        with_vectors=False,
    )
    for point in points:
        payload = point.payload or {}
        if payload.get("embed_mode") == "per_phrase":
            return True
    return False


def knowledge_needs_rebuild(settings: Settings) -> bool:
    """True when collection is missing or disk manuals / FAQ DB are not indexed."""
    disk_files = _iter_knowledge_files(settings)
    disk_names = {path.name for path in disk_files}
    store = _faq_store(settings)
    has_enabled_faq = store.count_enabled() > 0

    if not disk_names and not has_enabled_faq:
        return False
    if not vectorstore_exists(settings):
        return True
    indexed = _indexed_source_names(settings)
    if disk_names and not disk_names.issubset(indexed):
        return True
    # Old indexes without image binding need a one-time rebuild for docx manuals.
    has_docx = any(path.suffix.lower() == ".docx" for path in disk_files)
    if has_docx and not _any_chunk_has_images(settings):
        return True
    if has_enabled_faq and not _any_chunk_has_faq_id(settings):
        return True
    if has_enabled_faq and not _any_chunk_has_per_phrase_faq(settings):
        return True
    return False


def ensure_vectorstore(settings: Settings) -> str | None:
    """Build or refresh vector store when source files change."""
    if not knowledge_needs_rebuild(settings):
        return None
    build_vectorstore(settings)
    return "知识库已自动同步（检测到新增/变更的 FAQ 或操作文档）。"


def _dedupe_faq_docs(docs: list[Document]) -> list[Document]:
    """Keep the best-scoring hit per faq_id (or per source+content for manuals)."""
    best: dict[str, Document] = {}
    order: list[str] = []
    for doc in docs:
        faq_id = str(doc.metadata.get("faq_id") or "").strip()
        if faq_id:
            key = f"faq:{faq_id}"
        else:
            source = str(doc.metadata.get("source") or "")
            key = f"doc:{source}:{hash(doc.page_content)}"
        score = float(doc.metadata.get("score") or 0.0)
        if key not in best:
            best[key] = doc
            order.append(key)
            continue
        prev = float(best[key].metadata.get("score") or 0.0)
        if score > prev:
            best[key] = doc
    return [best[key] for key in order]


def similarity_search(
    settings: Settings,
    query: str,
    k: int | None = None,
    score_threshold: float | None = None,
    doc_types: list[str] | None = None,
    *,
    apply_threshold: bool = True,
) -> tuple[list[Document], list[tuple[float, str, str]]]:
    """Search with scores.

    Args:
      doc_types: If set, only search these payload doc_type values (e.g. ["faq"]).
      apply_threshold: When False, return all Top raw hits without clarify filter.

    Returns:
      accepted_docs: score >= clarify threshold (unless apply_threshold=False),
        FAQ hits deduped by faq_id
      candidates: (score, label, doc_type) for every raw Top-K hit (for logging)
    """
    if not vectorstore_exists(settings):
        return [], []

    # Fetch extra raw hits so after FAQ dedupe we still have enough unique FAQs.
    limit = k or settings.top_k
    raw_limit = max(limit * 4, limit)
    threshold = (
        settings.clarify_threshold if score_threshold is None else score_threshold
    )

    client = _get_qdrant_client(settings)
    embeddings = _get_embeddings(settings)
    query_vector = embeddings.embed_query(query)

    query_filter = None
    if doc_types:
        if len(doc_types) == 1:
            query_filter = Filter(
                must=[
                    FieldCondition(
                        key="doc_type", match=MatchValue(value=doc_types[0])
                    )
                ]
            )
        else:
            query_filter = Filter(
                should=[
                    FieldCondition(key="doc_type", match=MatchValue(value=t))
                    for t in doc_types
                ]
            )

    results = client.query_points(
        collection_name=settings.collection_name,
        query=query_vector,
        query_filter=query_filter,
        limit=raw_limit,
        with_payload=True,
    )

    accepted: list[Document] = []
    candidates: list[tuple[float, str, str]] = []

    for point in results.points:
        score = float(point.score) if point.score is not None else 0.0
        payload = point.payload or {}
        page_content = str(payload.get(CONTENT_KEY, ""))
        metadata = {
            key: value for key, value in payload.items() if key != CONTENT_KEY
        }
        doc_type = str(metadata.get("doc_type") or "")
        question = str(metadata.get("question") or "").strip()
        match_text = str(metadata.get("match_text") or page_content).strip()
        source = str(metadata.get("source", "未知来源"))
        label = match_text or question or Path(source).name
        candidates.append((score, label, doc_type or "unknown"))

        if apply_threshold and score < threshold:
            continue

        metadata["score"] = score
        metadata["vector_score"] = score
        accepted.append(Document(page_content=page_content, metadata=metadata))

    accepted = _dedupe_faq_docs(accepted)[: (raw_limit if not apply_threshold else limit)]
    return accepted, candidates[:raw_limit]


def search_faq(
    settings: Settings,
    query: str,
    k: int | None = None,
) -> tuple[list[Document], list[tuple[float, str, str]], str]:
    """Retrieve FAQ via hybrid (vector+BM25) and optional DashScope rerank."""
    limit = k or settings.top_k
    # Raw vector hits (no threshold) for hybrid pool
    vector_docs, vector_candidates = similarity_search(
        settings,
        query,
        k=max(limit, settings.hybrid_vector_k),
        doc_types=["faq"],
        apply_threshold=False,
    )

    if not settings.hybrid_search:
        # Legacy path: vector + clarify threshold only
        accepted = [
            d
            for d in vector_docs
            if float((d.metadata or {}).get("score") or 0.0)
            >= settings.clarify_threshold
        ]
        accepted = _dedupe_faq_docs(accepted)[:limit]
        if accepted:
            return accepted, list(vector_candidates), "faq"
        return [], list(vector_candidates), "none"

    from src.faq_store import FaqStore
    from src.retrieve import hybrid_faq_retrieve

    faq_store = FaqStore(settings.faq_db_path)
    accepted, display, debug = hybrid_faq_retrieve(
        settings,
        query,
        vector_docs=vector_docs,
        vector_candidates=vector_candidates,
        faq_store=faq_store,
    )
    # Stash debug on first doc for logging (chatbot can read if needed)
    if accepted:
        meta = dict(accepted[0].metadata or {})
        meta["_retrieve_debug"] = debug
        accepted[0] = Document(
            page_content=accepted[0].page_content, metadata=meta
        )
        return _dedupe_faq_docs(accepted)[:limit], display, "faq"
    return [], display or list(vector_candidates), "none"


# Backward-compatible alias used by older call sites / docs.
search_faq_then_manual = search_faq


def _payload_for_chunk(chunk: Document) -> dict:
    payload: dict = {CONTENT_KEY: chunk.page_content}
    for key, value in (chunk.metadata or {}).items():
        if key == CONTENT_KEY:
            continue
        if key == "images":
            if isinstance(value, list):
                payload["images"] = [str(item) for item in value]
            elif isinstance(value, str) and value:
                payload["images"] = [value]
            else:
                payload["images"] = []
            continue
        # Qdrant payload values should be JSON-serializable scalars / lists.
        if isinstance(value, (str, int, float, bool)) or value is None:
            payload[key] = value
        else:
            payload[key] = str(value)
    return payload


def _upsert_chunks(
    client: QdrantClient,
    settings: Settings,
    chunks: list,
    embeddings: OpenAIEmbeddings,
) -> None:
    if not chunks:
        return

    batches = [
        chunks[i : i + _EMBED_BATCH_SIZE]
        for i in range(0, len(chunks), _EMBED_BATCH_SIZE)
    ]

    def _embed_one(batch: list) -> tuple[list, list]:
        texts = [chunk.page_content for chunk in batch]
        vectors = embeddings.embed_documents(texts)
        return batch, vectors

    # Parallelize embedding HTTP calls across batches; upsert sequentially.
    workers = max(1, min(_EMBED_MAX_WORKERS, len(batches)))
    embedded: list[tuple[list, list]] = []
    if workers == 1:
        embedded = [_embed_one(batch) for batch in batches]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_embed_one, batch) for batch in batches]
            for fut in as_completed(futures):
                embedded.append(fut.result())

    for batch, vectors in embedded:
        points = [
            PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload=_payload_for_chunk(chunk),
            )
            for chunk, vector in zip(batch, vectors)
        ]
        client.upsert(
            collection_name=settings.collection_name,
            points=points,
        )


def _collection_vector_size(client: QdrantClient, name: str) -> int | None:
    if not _collection_exists(client, name):
        return None
    try:
        info = client.get_collection(name)
        vectors = info.config.params.vectors
        if hasattr(vectors, "size"):
            return int(vectors.size)
        if isinstance(vectors, dict) and vectors:
            first = next(iter(vectors.values()))
            return int(first.size)
    except Exception:
        return None
    return None


def _ensure_collection(
    client: QdrantClient, settings: Settings, vector_size: int, recreate: bool = False
) -> None:
    exists = _collection_exists(client, settings.collection_name)
    if exists and recreate:
        client.delete_collection(settings.collection_name)
        exists = False
    if not exists:
        client.create_collection(
            collection_name=settings.collection_name,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )


def delete_faq_vectors(settings: Settings, faq_ids: list[str]) -> None:
    """Remove all Qdrant points for the given faq_id values."""
    cleaned = [str(i).strip() for i in faq_ids if str(i).strip()]
    if not cleaned:
        return

    with _vector_lock:
        if not vectorstore_exists(settings):
            return
        client = _get_qdrant_client(settings)
        for faq_id in cleaned:
            client.delete(
                collection_name=settings.collection_name,
                points_selector=FilterSelector(
                    filter=Filter(
                        must=[
                            FieldCondition(
                                key="faq_id",
                                match=MatchValue(value=faq_id),
                            )
                        ]
                    )
                ),
            )


def sync_faq_vectors(settings: Settings, row: FaqRow | None, *, faq_id: str) -> None:
    """Incremental sync: delete old vectors for faq_id, then upsert if enabled."""
    faq_id = (faq_id or "").strip()
    if not faq_id:
        return

    delete_faq_vectors(settings, [faq_id])
    if row is None or not row.enabled:
        return

    upsert_faq_documents(settings, _faq_row_to_documents(row, settings))


def upsert_faq_documents(settings: Settings, docs: list[Document]) -> int:
    """Embed and upsert FAQ documents (batches + parallel embed HTTP)."""
    if not docs:
        return 0

    with _vector_lock:
        client = _get_qdrant_client(settings)
        embeddings = _get_embeddings(settings)
        vector_size = _collection_vector_size(client, settings.collection_name)
        if vector_size is None:
            vector_size = len(embeddings.embed_query("dimension probe"))
        _ensure_collection(client, settings, vector_size, recreate=False)
        _upsert_chunks(client, settings, docs, embeddings)
    return len(docs)


def upsert_faq_ids(settings: Settings, faq_ids: list[str]) -> int:
    """Load enabled FAQs by id and batch-embed them (for import)."""
    store = _faq_store(settings)
    docs: list[Document] = []
    for faq_id in faq_ids:
        row = store.get(str(faq_id).strip())
        if row is None or not row.enabled:
            continue
        docs.extend(_faq_row_to_documents(row, settings))
    return upsert_faq_documents(settings, docs)


def sync_faq_ids(settings: Settings, faq_ids: list[str]) -> None:
    """Re-sync multiple FAQ ids: delete old vectors, then batch upsert."""
    cleaned = [str(i).strip() for i in faq_ids if str(i).strip()]
    if not cleaned:
        return
    delete_faq_vectors(settings, cleaned)
    upsert_faq_ids(settings, cleaned)


def build_vectorstore(settings: Settings, source_dir: Path | None = None) -> None:
    if source_dir is not None:
        documents = _load_documents(source_dir, settings)
        empty_hint = str(source_dir)
    else:
        documents = _load_all_documents(settings)
        empty_hint = f"FAQ 库 {settings.faq_db_path} 或 {settings.docs_dir}"

    chunks = _split_documents(documents, settings)
    if not chunks:
        raise ValueError(f"知识库目录为空或无法读取：{empty_hint}")

    with _vector_lock:
        client = _get_qdrant_client(settings)
        embeddings = _get_embeddings(settings)
        sample_vector = embeddings.embed_query("dimension probe")
        _ensure_collection(client, settings, len(sample_vector), recreate=True)
        _upsert_chunks(client, settings, chunks, embeddings)


def _normalize_upload_path(file_path: str | Path) -> Path:
    if hasattr(file_path, "name") and not isinstance(file_path, (str, Path)):
        return Path(getattr(file_path, "name"))
    return Path(file_path)


def ingest_uploaded_files(settings: Settings, file_paths: list[str]) -> int:
    """Add uploaded files into the existing vector store."""
    if not file_paths:
        return 0

    new_docs: list[Document] = []
    for file_path in file_paths:
        path = _normalize_upload_path(file_path)
        if not path.exists():
            continue
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        # Persist uploads into docs/ so later rebuilds keep them.
        if path.suffix.lower() == ".docx":
            target = settings.docs_dir / path.name
            settings.docs_dir.mkdir(parents=True, exist_ok=True)
            if path.resolve() != target.resolve():
                target.write_bytes(path.read_bytes())
            path = target
        new_docs.extend(_load_file(path, settings))

    chunks = _split_documents(new_docs, settings)
    if not chunks:
        return 0

    # If uploads were copied into data/docs, a full rebuild indexes them cleanly.
    disk_names = {path.name for path in _iter_knowledge_files(settings)}
    upload_names = {Path(d.metadata.get("source", "")).name for d in new_docs}
    if upload_names and upload_names.issubset(disk_names):
        build_vectorstore(settings)
        return len(chunks)

    with _vector_lock:
        client = _get_qdrant_client(settings)
        embeddings = _get_embeddings(settings)
        vector_size = _collection_vector_size(client, settings.collection_name)
        if vector_size is None:
            vector_size = len(embeddings.embed_query("dimension probe"))

        if not vectorstore_exists(settings):
            try:
                # build_vectorstore takes the same RLock (re-entrant).
                build_vectorstore(settings)
                return len(chunks)
            except ValueError:
                _ensure_collection(client, settings, vector_size, recreate=False)

        if not vectorstore_exists(settings):
            _ensure_collection(client, settings, vector_size, recreate=False)

        _upsert_chunks(client, settings, chunks, embeddings)
    return len(chunks)
