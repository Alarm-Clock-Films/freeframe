"""add indexes for lookups that had none

Each of these columns is filtered on in a request path and had no index, so
the query read the whole table:

  * annotations.comment_id, comment_attachments.comment_id: every comment
    listing loads the annotations and attachments of the comments on the page.
  * asset_shares.asset_id, asset_shares.shared_with_user_id: every access
    check for someone who is not a project member, and "shared with me". The
    model has declared the asset_id index all along; no migration created it.
  * share_link_items.asset_id, share_link_items.folder_id: deleting an asset
    or a folder removes the share-link items that name it.
  * share_links.asset_id: listing an asset's share links. Declared on the
    model all along, like asset_shares.asset_id, and never created.
  * lower(users.email): sign-in falls back to a case-insensitive match, and
    inviting a user checks for one.

IF NOT EXISTS, because an instance may have added some of these by hand.

Revision ID: d7e2a4c6f8b1
Revises: c5d1f3a7b9e2
Create Date: 2026-11-12
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'd7e2a4c6f8b1'
down_revision: Union[str, Sequence[str], None] = 'c5d1f3a7b9e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index name, table, columns)
_COLUMN_INDEXES = (
    ('ix_annotations_comment_id', 'annotations', ['comment_id']),
    ('ix_comment_attachments_comment_id', 'comment_attachments', ['comment_id']),
    ('ix_asset_shares_asset_id', 'asset_shares', ['asset_id']),
    ('ix_asset_shares_shared_with_user_id', 'asset_shares', ['shared_with_user_id']),
    ('ix_share_link_items_asset_id', 'share_link_items', ['asset_id']),
    ('ix_share_link_items_folder_id', 'share_link_items', ['folder_id']),
    ('ix_share_links_asset_id', 'share_links', ['asset_id']),
)


def upgrade() -> None:
    for name, table, columns in _COLUMN_INDEXES:
        op.create_index(name, table, columns, unique=False, if_not_exists=True)
    op.create_index(
        'ix_users_email_lower', 'users', [sa.text('lower(email)')], unique=False, if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index('ix_users_email_lower', table_name='users', if_exists=True)
    for name, table, _columns in reversed(_COLUMN_INDEXES):
        op.drop_index(name, table_name=table, if_exists=True)
