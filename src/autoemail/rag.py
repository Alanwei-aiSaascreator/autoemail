"""轻量 case-scoped 检索。MVP 使用 token overlap，不跨 case。"""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class KnowledgeDocument:
    doc_id: str
    title: str
    text: str
    source: str
    case_id: int | None = None
    allowed_intents: tuple[str, ...] = ()
    sensitivity: str = "internal"


@dataclass(frozen=True)
class RetrievedDocument:
    doc_id: str
    title: str
    text: str
    source: str
    score: float


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9_/-]+|[\u4e00-\u9fff]", text.lower()) if token}


class InMemoryRetriever:
    def __init__(self, documents: list[KnowledgeDocument] | None = None):
        self.documents = documents or []
        self.last_filtered = 0

    def search(self, *, query: str, case_id: int, intent: str, limit: int = 5) -> list[RetrievedDocument]:
        query_tokens = _tokens(query)
        candidates = []
        filtered = 0
        for doc in self.documents:
            if doc.case_id is not None and doc.case_id != case_id:
                filtered += 1
                continue
            if doc.allowed_intents and intent not in doc.allowed_intents:
                filtered += 1
                continue
            score = len(query_tokens & _tokens(f"{doc.title} {doc.text}"))
            if score:
                candidates.append((score, doc))
        self.last_filtered = filtered
        candidates.sort(key=lambda item: (-item[0], item[1].doc_id))
        return [RetrievedDocument(d.doc_id, d.title, d.text, d.source, float(score)) for score, d in candidates[:limit]]
