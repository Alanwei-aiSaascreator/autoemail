from autoemail.rag import InMemoryRetriever, KnowledgeDocument


def test_retriever_enforces_case_scope():
    retriever = InMemoryRetriever([
        KnowledgeDocument("global", "Bond FAQ", "application POA process", "internal"),
        KnowledgeDocument("case-a", "A", "application for company A", "case", case_id=1),
        KnowledgeDocument("case-b", "B", "application for company B", "case", case_id=2),
    ])
    results = retriever.search(query="application", case_id=1, intent="form_request")
    ids = {item.doc_id for item in results}
    assert "global" in ids
    assert "case-a" in ids
    assert "case-b" not in ids


def test_retriever_filters_intent():
    retriever = InMemoryRetriever([
        KnowledgeDocument("quote", "Quote", "price quote", "internal", allowed_intents=("quote_reply",)),
        KnowledgeDocument("form", "Form", "application POA", "internal", allowed_intents=("form_request",)),
    ])
    results = retriever.search(query="application", case_id=1, intent="quote_reply")
    assert [item.doc_id for item in results] == []
