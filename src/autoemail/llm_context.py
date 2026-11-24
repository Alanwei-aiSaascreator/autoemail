"""给模型的受限上下文构建器：结构化事实与 thread 历史分层。"""
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Case, Commission, Agent, AgentPolicy, Message
from .noise import clean_body, recent_history
from .rag import InMemoryRetriever, RetrievedDocument


@dataclass
class StructuredContext:
    case_facts: dict
    current_message: dict
    thread_history: list[dict]
    references: list[RetrievedDocument] = field(default_factory=list)


def build_context(session: Session, *, case: Case, thread_id: int, source_message: Message, retriever: InMemoryRetriever | None = None, intent: str = "unclear") -> StructuredContext:
    commission = session.get(Commission, case.commission_id)
    agent = session.get(Agent, case.agent_id)
    policy = session.scalar(select(AgentPolicy).where(AgentPolicy.agent_id == case.agent_id))
    messages = session.scalars(select(Message).where(Message.thread_id == thread_id).order_by(Message.sent_at.asc(), Message.id.asc())).all()
    history = recent_history(messages)
    facts = {
        "case_no": case.case_no,
        "state": case.state,
        "commission_name": commission.legal_name if commission else None,
        "entity_type": commission.entity_type if commission else None,
        "agent_name": agent.name if agent else None,
        "agent_policy": {
            "materials": policy.material_checklist if policy else [],
            "invoice_timing": policy.invoice_timing if policy else None,
        },
    }
    references = retriever.search(query=clean_body(source_message.body_text, limit=4000), case_id=case.id, intent=intent) if retriever else []
    return StructuredContext(
        case_facts=facts,
        current_message={"subject": source_message.subject, "body": clean_body(source_message.body_text, limit=12000)},
        thread_history=history,
        references=references,
    )
