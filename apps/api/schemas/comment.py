import json
from typing import Annotated
from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator
import uuid
from datetime import datetime
from typing import Optional
from ..models.comment import CommentVisibility
from ..services.avatar_keys import sign_avatar
from ..config import settings


def _check_comment_body(value: str) -> str:
    # Read when a request is checked, not when this module loads, so the
    # setting can be changed without touching the schema.
    if len(value) > settings.comment_max_chars:
        raise ValueError(f"A comment can be at most {settings.comment_max_chars} characters")
    return value


# The text of a comment, as accepted from a request. Stored comments are not
# re-checked: CommentResponse.body stays a plain str.
CommentBody = Annotated[str, AfterValidator(_check_comment_body)]

# The longest list of explicit mentions one comment may carry.
MAX_MENTIONS_PER_COMMENT = 100

class AnnotationData(BaseModel):
    drawing_data: dict  # Fabric.js canvas JSON
    frame_number: Optional[int] = None
    carousel_position: Optional[int] = None

    @field_validator("drawing_data")
    @classmethod
    def _check_drawing_size(cls, value: dict) -> dict:
        # The drawing is stored as sent and returned with every comment
        # listing, so its size is bounded where it comes in.
        size = len(json.dumps(value, separators=(",", ":")).encode("utf-8"))
        if size > settings.annotation_max_bytes:
            raise ValueError(f"An annotation's drawing can be at most {settings.annotation_max_bytes} bytes")
        return value

class CommentCreate(BaseModel):
    version_id: uuid.UUID
    parent_id: Optional[uuid.UUID] = None
    timecode_start: Optional[float] = None
    timecode_end: Optional[float] = None
    body: CommentBody
    visibility: Optional[str] = "public"  # "public" or "internal"
    annotation: Optional[AnnotationData] = None
    # Explicit mention IDs from frontend
    mention_user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_MENTIONS_PER_COMMENT)

class GuestCommentCreate(BaseModel):
    asset_id: Optional[uuid.UUID] = None  # Required for folder/project shares
    version_id: Optional[uuid.UUID] = None  # Auto-resolved if not provided
    parent_id: Optional[uuid.UUID] = None
    timecode_start: Optional[float] = None
    timecode_end: Optional[float] = None
    body: CommentBody
    # "internal" is accepted only from a signed-in caller who can open the asset.
    visibility: CommentVisibility = CommentVisibility.public
    annotation: Optional[AnnotationData] = None
    # Both are stored in String(255) columns; a longer value was a 500.
    guest_email: Optional[str] = Field(default=None, max_length=255)  # Not needed if user is logged in
    guest_name: Optional[str] = Field(default=None, max_length=255)

class CommentUpdate(BaseModel):
    body: CommentBody

class AnnotationResponse(BaseModel):
    id: uuid.UUID
    comment_id: uuid.UUID
    drawing_data: dict
    frame_number: Optional[int]
    carousel_position: Optional[int]
    model_config = {"from_attributes": True}

# ── Attachments ────────────────────────────────────────────────────────────────

class AttachmentUploadRequest(BaseModel):
    # Bounded because file_name is stored verbatim as original_filename and
    # later echoed into a Content-Disposition header on the download URL.
    file_name: str = Field(min_length=1, max_length=255)
    file_size: int = Field(ge=0)
    content_type: str = Field(max_length=255)

    @field_validator("file_size")
    @classmethod
    def _check_file_size(cls, value: int) -> int:
        # The size the client declares. Storage is not asked to enforce it:
        # the upload goes straight to the bucket on a presigned URL.
        if value > settings.comment_attachment_max_bytes:
            raise ValueError(
                f"An attachment can be at most {settings.comment_attachment_max_bytes} bytes"
            )
        return value

class AttachmentUploadResponse(BaseModel):
    upload_url: str
    attachment_id: uuid.UUID
    key: str

class AttachmentResponse(BaseModel):
    id: uuid.UUID
    file_name: str
    file_size: int
    content_type: str
    url: str  # presigned S3 GET URL, generated at response time

# ── Reactions ──────────────────────────────────────────────────────────────────

class ReactionCreate(BaseModel):
    emoji: str

    @field_validator("emoji")
    @classmethod
    def emoji_max_length(cls, v: str) -> str:
        if len(v) > 10:
            raise ValueError("emoji must be at most 10 characters")
        return v

class ReactionResponse(BaseModel):
    emoji: str
    count: int
    reacted: bool  # whether the current user has reacted with this emoji

# ── Author info ────────────────────────────────────────────────────────────────

class AuthorInfo(BaseModel):
    id: uuid.UUID
    name: str
    avatar_url: Optional[str] = None
    avatar_color: Optional[str] = None

    @model_validator(mode="after")
    def resolve_avatar_url(self) -> "AuthorInfo":
        # Same rule as UserResponse: sign only this author's own uploaded
        # avatar, and show anything else as no avatar.
        self.avatar_url = sign_avatar(self.id, self.avatar_url)
        return self

class GuestAuthorInfo(BaseModel):
    id: uuid.UUID
    name: str
    email: str

# ── Comments ───────────────────────────────────────────────────────────────────

class CommentResponse(BaseModel):
    id: uuid.UUID
    asset_id: uuid.UUID
    version_id: uuid.UUID
    parent_id: Optional[uuid.UUID]
    author_id: Optional[uuid.UUID]
    guest_author_id: Optional[uuid.UUID]
    timecode_start: Optional[float]
    timecode_end: Optional[float]
    body: str
    resolved: bool
    visibility: str = "public"
    created_at: datetime
    updated_at: datetime
    author: Optional[AuthorInfo] = None
    guest_author: Optional[GuestAuthorInfo] = None
    annotation: Optional[AnnotationResponse] = None
    replies: list["CommentResponse"] = []
    attachments: list[AttachmentResponse] = []
    reactions: list[ReactionResponse] = []
    model_config = {"from_attributes": True}

CommentResponse.model_rebuild()
