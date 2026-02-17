#!/usr/bin/env python3
"""Seed development database with realistic sample data.

Creates:
  - 2 tenants: "TE Connectivity" and "Demo Corp"
  - 3 users per tenant: admin, operator, viewer
  - 2 sample conversations with messages per tenant
  - 3-5 manufacturing-domain documents with chunks in the RAG index
  - Sample feedback entries linked to conversations
  - Prints dev JWT tokens for every user

Idempotent: safe to run multiple times - existing records identified by
slug/email are skipped rather than duplicated.

Usage:
    # From project root (database must be running and migrated)
    python scripts/seed.py

    # Or via make:
    make seed
"""

from __future__ import annotations

import asyncio
import math
import random
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path so "src.*" imports work whether
# this script is run directly or via "python scripts/seed.py".
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Sample manufacturing documents for the RAG index
# ---------------------------------------------------------------------------
SAMPLE_DOCUMENTS: list[dict] = [
    {
        "filename": "connector_quality_standards.txt",
        "title": "Connector Quality Standards v3.2",
        "content": (
            "TE Connectivity Connector Quality Standards v3.2\n\n"
            "1. Dimensional Tolerance\n"
            "All connector housings must conform to IPC-7251 land pattern standards. "
            "Dimensional tolerances are specified as ±0.05 mm for critical features "
            "and ±0.10 mm for non-critical features. Verify with CMM measurement.\n\n"
            "2. Contact Resistance\n"
            "Maximum allowable contact resistance is 20 mΩ at 100 mA test current. "
            "Measure with four-wire Kelvin method. Connectors exceeding this threshold "
            "must be quarantined and reviewed by the quality team.\n\n"
            "3. Mating Cycle Life\n"
            "Standard connectors are rated for a minimum of 500 mating cycles. "
            "High-reliability variants must achieve 2,000 cycles without degradation "
            "exceeding 50% of initial contact force specification.\n\n"
            "4. Environmental Compliance\n"
            "All products must comply with RoHS 3 (EU 2015/863) and REACH regulations. "
            "Halogen-free materials are required for automotive applications. "
            "Certificates of conformance must be maintained for 10 years."
        ),
        "content_type": "text/plain",
    },
    {
        "filename": "assembly_line_sop_2024.txt",
        "title": "Assembly Line Standard Operating Procedure 2024",
        "content": (
            "Assembly Line SOP - Revision 2024-Q4\n\n"
            "Pre-Shift Checklist:\n"
            "  1. Verify torque wrench calibration (due date on tool tag)\n"
            "  2. Confirm parts kitting against BOM revision\n"
            "  3. Check ESD wrist strap resistance (must be 750 kΩ – 10 MΩ)\n"
            "  4. Validate pick-and-place feeder positions against setup sheet\n\n"
            "Process Steps:\n"
            "  Step 1: Load PCB into fixture. Confirm orientation via silkscreen marker.\n"
            "  Step 2: Apply solder paste using stencil. Inspect paste volume with SPI.\n"
            "  Step 3: Place SMD components. Verify polarity for diodes and capacitors.\n"
            "  Step 4: Reflow soldering. Profile: preheat 150°C/60s, peak 245°C/10s.\n"
            "  Step 5: AOI inspection. Reject rate target < 0.5%.\n"
            "  Step 6: Wave solder THT components. Flux application critical.\n"
            "  Step 7: In-Circuit Test (ICT). All nets verified at full test coverage.\n"
            "  Step 8: Functional test at nominal and ±10% supply voltage.\n\n"
            "Non-Conformance Handling:\n"
            "Defects found at any step trigger a MRB (Material Review Board) tag. "
            "Suspected systemic issues require immediate line stop and supervisor notification."
        ),
        "content_type": "text/plain",
    },
    {
        "filename": "predictive_maintenance_guide.txt",
        "title": "Predictive Maintenance AI Integration Guide",
        "content": (
            "Predictive Maintenance AI Integration Guide\n\n"
            "Overview:\n"
            "This guide describes integration of the enterprise AI platform with the "
            "factory SCADA/MES system for predictive maintenance use cases. The AI "
            "agent monitors sensor streams and predicts equipment failures 24-72 hours "
            "in advance, enabling proactive maintenance scheduling.\n\n"
            "Sensor Data Requirements:\n"
            "  - Vibration: 3-axis accelerometer at 10 kHz sample rate\n"
            "  - Temperature: Thermocouple on bearings and motor windings\n"
            "  - Current draw: Hall-effect sensor on motor phase leads\n"
            "  - Oil pressure: Pressure transducer with 0-10 V output\n\n"
            "AI Model Architecture:\n"
            "The system uses a transformer-based anomaly detection model trained on "
            "18 months of historical sensor data. Features are extracted over 5-minute "
            "rolling windows. Alerts are generated when anomaly score exceeds 0.85.\n\n"
            "Integration Steps:\n"
            "  1. Configure MQTT broker to publish sensor topics to the AI gateway.\n"
            "  2. Map sensor IDs to equipment registry in the platform.\n"
            "  3. Set alert thresholds and escalation rules in the AI dashboard.\n"
            "  4. Validate alert pipeline with a known fault simulation.\n\n"
            "Alert Response Protocol:\n"
            "Tier 1 alerts (score 0.85-0.92): Schedule maintenance within 72 hours.\n"
            "Tier 2 alerts (score 0.93-0.97): Schedule within 24 hours.\n"
            "Tier 3 alerts (score >= 0.98): Immediate operator notification, prepare standby."
        ),
        "content_type": "text/plain",
    },
    {
        "filename": "supply_chain_disruption_playbook.txt",
        "title": "Supply Chain Disruption Response Playbook",
        "content": (
            "Supply Chain Disruption Response Playbook v1.1\n\n"
            "Scope: This playbook applies to Tier-1 and Tier-2 component shortages "
            "affecting production lines. It is triggered when on-hand inventory falls "
            "below the 14-day buffer threshold for any critical component.\n\n"
            "Severity Classification:\n"
            "  Level 1 (Advisory): 10-14 day supply remaining. Monitor daily.\n"
            "  Level 2 (Watch): 7-10 day supply. Activate alternate sourcing.\n"
            "  Level 3 (Critical): <7 day supply. Escalate to VP Operations.\n"
            "  Level 4 (Emergency): <3 day supply or supply already interrupted.\n\n"
            "Response Actions by Level:\n"
            "Level 2:\n"
            "  - Contact 3 alternate approved suppliers for spot quotes.\n"
            "  - Review open orders for pull-in opportunities.\n"
            "  - Evaluate design-equivalent substitutions with engineering.\n\n"
            "Level 3:\n"
            "  - Convene daily supply chain war room.\n"
            "  - Prioritize production schedule to highest-margin products.\n"
            "  - Notify key customers of potential delays per contract SLA terms.\n\n"
            "Level 4:\n"
            "  - Halt non-critical production lines.\n"
            "  - Activate emergency inventory from strategic reserve.\n"
            "  - CEO and board notification per materiality thresholds."
        ),
        "content_type": "text/plain",
    },
    {
        "filename": "ai_agent_usage_policy.txt",
        "title": "Enterprise AI Agent Usage Policy",
        "content": (
            "Enterprise AI Agent Usage Policy v2.0\n\n"
            "1. Permitted Use Cases\n"
            "The enterprise AI platform may be used for: quality data analysis, "
            "SOP lookup and summarization, predictive maintenance insights, "
            "supply chain Q&A, and engineering document search. All use must comply "
            "with applicable data classification requirements.\n\n"
            "2. Data Handling\n"
            "Do not input: personally identifiable information (PII), financial data "
            "classified as confidential, unreleased product designs, or any data "
            "marked ITAR/EAR controlled. Customer names may be used only in anonymised form.\n\n"
            "3. Output Review\n"
            "AI-generated outputs must not be used as sole basis for safety-critical decisions. "
            "Human review is mandatory before acting on maintenance recommendations that "
            "could affect personnel safety or product release decisions.\n\n"
            "4. Audit Trail\n"
            "All queries and responses are logged for 90 days. Unusual query patterns "
            "are flagged for security review. Users acknowledge that their activity "
            "may be audited.\n\n"
            "5. Feedback\n"
            "Users are encouraged to provide thumbs-up/thumbs-down ratings on responses. "
            "This feedback improves model quality and helps identify systematic errors. "
            "Specific improvement suggestions can be submitted via the feedback comment field."
        ),
        "content_type": "text/plain",
    },
]

SAMPLE_CONVERSATIONS: list[dict] = [
    {
        "title": "Connector resistance spec query",
        "messages": [
            {
                "role": "user",
                "content": "What is the maximum allowable contact resistance for our standard connectors?",
            },
            {
                "role": "assistant",
                "content": (
                    "Based on the Connector Quality Standards v3.2, the maximum allowable "
                    "contact resistance for standard connectors is **20 mΩ** measured at "
                    "100 mA test current using the four-wire Kelvin method. Connectors that "
                    "exceed this threshold must be quarantined and reviewed by the quality team."
                ),
                "model_used": "mock/dev-model",
                "token_count": 87,
            },
            {
                "role": "user",
                "content": "How do we handle connectors that fail this test?",
            },
            {
                "role": "assistant",
                "content": (
                    "Connectors exceeding the 20 mΩ threshold should be: "
                    "1) Immediately quarantined to prevent use in production, "
                    "2) Tagged with an MRB (Material Review Board) label, and "
                    "3) Submitted for quality team review. The quality team will determine "
                    "whether to scrap, rework, or escalate to the supplier for corrective action. "
                    "Document all non-conformances in the quality management system."
                ),
                "model_used": "mock/dev-model",
                "token_count": 112,
            },
        ],
    },
    {
        "title": "Predictive maintenance alert thresholds",
        "messages": [
            {
                "role": "user",
                "content": "What anomaly score triggers a Tier 2 maintenance alert?",
            },
            {
                "role": "assistant",
                "content": (
                    "According to the Predictive Maintenance AI Integration Guide, a "
                    "**Tier 2 alert** is triggered when the anomaly score is in the range "
                    "**0.93 – 0.97**. At this level, maintenance should be scheduled within "
                    "24 hours. For reference: Tier 1 (0.85-0.92) allows 72 hours, and "
                    "Tier 3 (>=0.98) requires immediate operator notification."
                ),
                "model_used": "mock/dev-model",
                "token_count": 98,
            },
        ],
    },
]


def _make_fake_embedding(dim: int = 1536) -> list[float]:
    """Generate a unit-normalised random embedding vector for dev data."""
    vec = [random.gauss(0, 1) for _ in range(dim)]
    magnitude = math.sqrt(sum(x * x for x in vec))
    return [x / magnitude for x in vec]


def _chunk_text(text: str, chunk_size: int = 400, overlap: int = 50) -> list[str]:
    """Split text into overlapping word-based chunks."""
    words = text.split()
    chunks: list[str] = []
    step = max(1, chunk_size - overlap)
    for i in range(0, len(words), step):
        chunk = " ".join(words[i : i + chunk_size])
        if chunk:
            chunks.append(chunk)
        if i + chunk_size >= len(words):
            break
    return chunks


async def seed() -> None:
    """Main seed routine - idempotent."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from src.auth.oidc import create_dev_token
    from src.config import get_settings
    from src.database import get_engine, init_db as _init_engine
    import src.models  # noqa: F401 - register all ORM classes with metadata
    from src.models.conversation import Conversation, Message, MessageRole
    from src.models.document import Document, DocumentChunk, DocumentStatus
    from src.models.feedback import FeedbackRating, ResponseFeedback
    from src.models.tenant import Tenant
    from src.models.user import User, UserRole

    settings = get_settings()
    _init_engine(settings)
    engine = get_engine()
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    secret = settings.dev_jwt_secret.get_secret_value()

    tenants_data = [
        {
            "name": "TE Connectivity",
            "slug": "te-connectivity",
            "description": "Global leader in connectivity and sensor solutions for harsh environments.",
        },
        {
            "name": "Demo Corp",
            "slug": "demo-corp",
            "description": "Demo tenant for onboarding and feature evaluation.",
        },
    ]

    printed_tokens: list[dict] = []

    async with session_factory() as db:
        # ------------------------------------------------------------------
        # Tenants
        # ------------------------------------------------------------------
        created_tenants: list[Tenant] = []
        for t_data in tenants_data:
            result = await db.execute(select(Tenant).where(Tenant.slug == t_data["slug"]))
            tenant = result.scalar_one_or_none()
            if tenant is None:
                tenant = Tenant(
                    name=t_data["name"],
                    slug=t_data["slug"],
                    description=t_data["description"],
                )
                db.add(tenant)
                await db.flush()
                print(f"  [+] Tenant created: {tenant.slug} ({tenant.id})")
            else:
                print(f"  [~] Tenant exists:  {tenant.slug} ({tenant.id})")
            created_tenants.append(tenant)

        # ------------------------------------------------------------------
        # Users (admin, operator, viewer per tenant)
        # ------------------------------------------------------------------
        created_users: dict[str, list[User]] = {}  # tenant_slug -> list[User]
        for tenant in created_tenants:
            created_users[tenant.slug] = []
            roles_to_create = [UserRole.ADMIN, UserRole.OPERATOR, UserRole.VIEWER]
            for role in roles_to_create:
                email = f"{role.value}@{tenant.slug}.dev"
                result = await db.execute(
                    select(User).where(
                        User.tenant_id == tenant.id,
                        User.email == email,
                    )
                )
                user = result.scalar_one_or_none()
                if user is None:
                    sub = str(uuid.uuid4())
                    user = User(
                        tenant_id=tenant.id,
                        external_id=sub,
                        email=email,
                        display_name=f"{tenant.name} {role.value.title()}",
                        role=role,
                    )
                    db.add(user)
                    await db.flush()
                    print(f"  [+] User created: {email} (role={role.value})")
                else:
                    print(f"  [~] User exists:  {email}")

                created_users[tenant.slug].append(user)

                token = create_dev_token(
                    sub=user.external_id,
                    tenant_id=str(tenant.id),
                    role=role.value,
                    email=email,
                    secret=secret,
                    expires_in=86400 * 30,  # 30 days for dev convenience
                )
                printed_tokens.append(
                    {
                        "tenant": tenant.slug,
                        "role": role.value,
                        "email": email,
                        "token": token,
                    }
                )

        # ------------------------------------------------------------------
        # Documents + Chunks (seeded once per tenant)
        # ------------------------------------------------------------------
        for tenant in created_tenants:
            admin_user = next(
                u for u in created_users[tenant.slug] if u.role == UserRole.ADMIN
            )
            for doc_data in SAMPLE_DOCUMENTS:
                result = await db.execute(
                    select(Document).where(
                        Document.tenant_id == tenant.id,
                        Document.filename == doc_data["filename"],
                    )
                )
                doc = result.scalar_one_or_none()
                if doc is not None:
                    print(f"  [~] Document exists: {doc_data['filename']} ({tenant.slug})")
                    continue

                raw_content = doc_data["content"]
                doc = Document(
                    tenant_id=tenant.id,
                    uploaded_by_user_id=admin_user.id,
                    filename=doc_data["filename"],
                    content_type=doc_data["content_type"],
                    size_bytes=len(raw_content.encode()),
                    status=DocumentStatus.READY,
                    metadata_={"title": doc_data["title"], "source": "seed_script"},
                )
                db.add(doc)
                await db.flush()

                text_chunks = _chunk_text(raw_content)
                doc.chunk_count = len(text_chunks)
                for idx, chunk_text in enumerate(text_chunks):
                    chunk = DocumentChunk(
                        document_id=doc.id,
                        tenant_id=tenant.id,
                        content=chunk_text,
                        chunk_index=idx,
                        embedding=_make_fake_embedding(1536),
                        chunk_metadata={
                            "source": doc_data["filename"],
                            "title": doc_data["title"],
                            "chunk_index": idx,
                            "total_chunks": len(text_chunks),
                        },
                    )
                    db.add(chunk)

                await db.flush()
                print(
                    f"  [+] Document seeded: {doc_data['filename']} "
                    f"({len(text_chunks)} chunks, {tenant.slug})"
                )

        # ------------------------------------------------------------------
        # Conversations + Messages
        # ------------------------------------------------------------------
        for tenant in created_tenants:
            operator_user = next(
                u for u in created_users[tenant.slug] if u.role == UserRole.OPERATOR
            )
            for conv_data in SAMPLE_CONVERSATIONS:
                result = await db.execute(
                    select(Conversation).where(
                        Conversation.tenant_id == tenant.id,
                        Conversation.user_id == operator_user.id,
                        Conversation.title == conv_data["title"],
                    )
                )
                conv = result.scalar_one_or_none()
                if conv is not None:
                    print(f"  [~] Conversation exists: {conv_data['title']!r} ({tenant.slug})")
                    continue

                conv = Conversation(
                    tenant_id=tenant.id,
                    user_id=operator_user.id,
                    title=conv_data["title"],
                    metadata_={"seeded": True},
                )
                db.add(conv)
                await db.flush()

                last_assistant_msg = None
                for seq, msg_data in enumerate(conv_data["messages"]):
                    role = MessageRole(msg_data["role"])
                    msg = Message(
                        conversation_id=conv.id,
                        tenant_id=tenant.id,
                        role=role,
                        content=msg_data["content"],
                        sequence_number=seq,
                        model_used=msg_data.get("model_used"),
                        token_count=msg_data.get("token_count"),
                        citations=[],
                        tool_calls=[],
                    )
                    db.add(msg)
                    await db.flush()
                    if role == MessageRole.ASSISTANT:
                        last_assistant_msg = msg

                # ----------------------------------------------------------
                # Feedback entry for the last assistant message
                # ----------------------------------------------------------
                if last_assistant_msg is not None:
                    result = await db.execute(
                        select(ResponseFeedback).where(
                            ResponseFeedback.tenant_id == tenant.id,
                            ResponseFeedback.conversation_id == conv.id,
                            ResponseFeedback.message_id == last_assistant_msg.id,
                        )
                    )
                    fb = result.scalar_one_or_none()
                    if fb is None:
                        viewer_user = next(
                            u
                            for u in created_users[tenant.slug]
                            if u.role == UserRole.VIEWER
                        )
                        feedback = ResponseFeedback(
                            tenant_id=tenant.id,
                            user_id=viewer_user.id,
                            conversation_id=conv.id,
                            message_id=last_assistant_msg.id,
                            rating=FeedbackRating.THUMBS_UP,
                            comment="Accurate and well-sourced answer.",
                            tags=["accurate", "helpful"],
                            prompt_text=conv_data["messages"][-2]["content"]
                            if len(conv_data["messages"]) >= 2
                            else "",
                            response_text=last_assistant_msg.content,
                            model_used=last_assistant_msg.model_used or "mock/dev-model",
                        )
                        db.add(feedback)
                        await db.flush()

                print(
                    f"  [+] Conversation seeded: {conv_data['title']!r} "
                    f"({len(conv_data['messages'])} messages, {tenant.slug})"
                )

        await db.commit()

    # ------------------------------------------------------------------
    # Print dev tokens to stdout
    # ------------------------------------------------------------------
    divider = "=" * 72
    print(f"\n{divider}")
    print("SEED COMPLETE - Development JWT tokens (valid 30 days):")
    print(divider)
    for entry in printed_tokens:
        print(
            f"\n  Tenant : {entry['tenant']}\n"
            f"  Role   : {entry['role']}\n"
            f"  Email  : {entry['email']}\n"
            f"  Token  : {entry['token']}"
        )
        print(
            f"\n  curl -s -H 'Authorization: Bearer {entry['token'][:60]}...' "
            "http://localhost:8000/api/v1/conversations | python3 -m json.tool"
        )
    print(f"\n{divider}")
    print("  API Docs : http://localhost:8000/docs")
    print("  Frontend : http://localhost:5173")
    print(f"{divider}\n")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
