"""Portable embeddings with self-validating, automatically rebuilt indexes."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

import numpy as np

from .storage import atomic_write_json

INDEX_VERSION = 1
HASH_DIMENSIONS = 768


class HashEmbedder:
    backend = "stable_hash"
    model = "blake2b-token-count-v1"
    dimensions = HASH_DIMENSIONS

    def encode(self, texts):
        vectors = np.zeros((len(texts), self.dimensions), dtype=np.float32)
        for row, text in enumerate(texts):
            for token in re.findall(r"\w{2,}", text.casefold(), flags=re.UNICODE):
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                vectors[row, int.from_bytes(digest, "big") % self.dimensions] += 1
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.maximum(norms, 1e-9)


class TransformerEmbedder:
    backend = "sentence_transformers"

    def __init__(self, model, allow_download):
        # Heavy dependency is imported only when retrieval is explicitly requested.
        from sentence_transformers import SentenceTransformer
        self.model = model
        self.encoder = SentenceTransformer(model, local_files_only=not allow_download)
        self.dimensions = int(self.encoder.get_sentence_embedding_dimension())

    def encode(self, texts):
        return np.asarray(self.encoder.encode(texts, normalize_embeddings=True), dtype=np.float32)


@lru_cache(maxsize=4)
def get_embedder(backend="hash", model="sentence-transformers/all-MiniLM-L6-v2", allow_download=False):
    if backend == "sentence_transformers":
        try:
            return TransformerEmbedder(model, allow_download), ""
        except Exception:
            return HashEmbedder(), "Semantic search is unavailable. Using local word-based search."
    return HashEmbedder(), ""


def read_documents(directory):
    directory = Path(directory)
    docs = []
    if not directory.is_dir():
        return docs
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.lower() in {".md", ".txt"}:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                docs.append({"id": path.relative_to(directory).as_posix(), "text": text})
    return docs


class KnowledgeSearch:
    def __init__(self, knowledge_dir, store_dir, backend="hash",
                 model="sentence-transformers/all-MiniLM-L6-v2", allow_download=False):
        self.knowledge_dir = Path(knowledge_dir)
        self.index_path = Path(store_dir) / "kb_index.json"
        self.backend, self.model, self.allow_download = backend, model, allow_download
        self._cached = None
        self._matrix_index = None

    def _signature(self):
        try:
            stat = self.index_path.stat()
            return stat.st_ino, stat.st_mtime_ns, stat.st_size
        except FileNotFoundError:
            return None

    def _metadata(self, docs, embedder):
        fingerprint = hashlib.sha256(json.dumps(docs, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        return {"version": INDEX_VERSION, "backend": embedder.backend,
                "model": embedder.model, "dimensions": embedder.dimensions,
                "corpus_sha256": fingerprint}

    def _valid(self, index, metadata, docs):
        if not isinstance(index, dict) or index.get("metadata") != metadata:
            return False
        stored = index.get("docs")
        if not isinstance(stored, list) or len(stored) != len(docs):
            return False
        try:
            for record, source in zip(stored, docs):
                if record["id"] != source["id"] or record["text"] != source["text"]:
                    return False
                vector = np.asarray(record["embedding"], dtype=np.float32)
                if vector.shape != (metadata["dimensions"],) or not np.isfinite(vector).all():
                    return False
        except (KeyError, TypeError, ValueError):
            return False
        return True

    def _ensure(self, docs, embedder, force=False):
        metadata = self._metadata(docs, embedder)
        signature = self._signature()
        if not force and self._cached and self._cached[:2] == (metadata, signature):
            return self._cached[2], False
        try:
            index = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            index = None
        if not force and self._valid(index, metadata, docs):
            self._cached = (metadata, signature, index)
            return index, False
        vectors = embedder.encode([doc["text"] for doc in docs]) if docs else []
        index = {"metadata": metadata, "docs": [
            {**doc, "embedding": vector.tolist()} for doc, vector in zip(docs, vectors)]}
        atomic_write_json(self.index_path, index)
        self._cached = (metadata, self._signature(), index)
        return index, True

    def retrieve(self, query, k=5, force=False):
        docs = read_documents(self.knowledge_dir)
        embedder, warning = get_embedder(self.backend, self.model, self.allow_download)
        try:
            index, rebuilt = self._ensure(docs, embedder, force)
            query_vector = embedder.encode([query])[0]
        except Exception:
            if embedder.backend != "sentence_transformers":
                raise
            # Also catch failures during encoding, not just model construction.
            embedder = HashEmbedder()
            warning = "Semantic search failed. Using local word-based search."
            index, rebuilt = self._ensure(docs, embedder, force=True)
            query_vector = embedder.encode([query])[0]
        if self._matrix_index is not index:
            self._matrix = np.asarray([doc["embedding"] for doc in index["docs"]], dtype=np.float32).reshape(-1, embedder.dimensions)
            self._norms = np.linalg.norm(self._matrix, axis=1)
            self._matrix_index = index
        denominators = self._norms * np.linalg.norm(query_vector)
        scores = np.divide(self._matrix @ query_vector, denominators,
                           out=np.zeros(len(index["docs"]), dtype=np.float32), where=denominators > 0)
        ranked = []
        for doc, score in zip(index["docs"], scores):
            score = float(score)
            if score > 0:
                ranked.append({"id": doc["id"], "text": doc["text"], "score": score})
        ranked.sort(key=lambda item: (-item["score"], item["id"]))
        return {"hits": ranked[:k], "rebuilt": rebuilt, "warning": warning,
                "backend": embedder.backend, "document_count": len(docs)}

    def rebuild(self):
        return self.retrieve("", force=True)
