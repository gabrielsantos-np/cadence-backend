"""GET /api/conversations — the sidebar list, and one full conversation."""

import logging

from fastapi import APIRouter, HTTPException

from cadence_backend.conversations import list_conversations, load_conversation
from cadence_backend.schemas.conversation import Conversation, ConversationList

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["conversations"])


@router.get(
    "/conversations",
    summary="List conversations, most recently updated first",
    response_model=ConversationList,
    response_model_exclude_none=True,
)
async def get_conversations() -> ConversationList:
    return ConversationList(conversations=await list_conversations())


# exclude_none matches what the stream sends: an absent optional key is
# omitted, never serialised as null. The frontend tests `marker !== undefined`,
# so a null marker passes that guard and then crashes reading marker.x.
@router.get(
    "/conversations/{conversation_id}",
    summary="Load one conversation with its full message history",
    response_model=Conversation,
    response_model_exclude_none=True,
)
async def get_conversation(conversation_id: str) -> Conversation:
    conversation = await load_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Not found")
    return conversation
