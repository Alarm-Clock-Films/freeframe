from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from ..database import get_db
from ..middleware.auth import get_current_user
from ..models.user import User, GuestUser
from ..models.asset import Asset
from ..models.project import Project
from ..models.activity import Notification
from ..models.comment import Comment
from ..schemas.asset import NotificationResponse
import uuid

router = APIRouter(tags=["notifications"])


@router.get("/me/notifications", response_model=list[NotificationResponse], operation_id="get_my_notifications")
def list_notifications(
    unread_only: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Only notifications about live content. Each one names its asset and quotes
    # its comment, so once the asset is in the trash or its project is deleted,
    # the notification is left out with it.
    query = (
        db.query(Notification)
        .join(Asset, Asset.id == Notification.asset_id)
        .join(Project, Project.id == Asset.project_id)
        .filter(
            Notification.user_id == current_user.id,
            Asset.deleted_at.is_(None),
            Project.deleted_at.is_(None),
        )
    )
    if unread_only:
        query = query.filter(Notification.read == False)
    notifications = query.order_by(Notification.created_at.desc()).limit(50).all()

    # Enrich with asset name, actor name, comment preview. Loaded together:
    # looked up per notification this was up to four queries for each of the
    # fifty, on a list the notification bell refreshes.
    def _by_id(model, ids):
        ids = {i for i in ids if i}
        return {row.id: row for row in db.query(model).filter(model.id.in_(ids)).all()} if ids else {}

    assets = _by_id(Asset, (n.asset_id for n in notifications))
    comments = _by_id(Comment, (n.comment_id for n in notifications))
    authors = _by_id(User, (c.author_id for c in comments.values()))
    guests = _by_id(GuestUser, (c.guest_author_id for c in comments.values() if not c.author_id))

    results = []
    for n in notifications:
        asset_name = None
        actor_name = None
        actor_is_guest = False
        comment_preview = None
        project_id = None

        asset = assets.get(n.asset_id)
        if asset:
            asset_name = asset.name
            project_id = asset.project_id

        comment = comments.get(n.comment_id)
        if comment:
            comment_preview = comment.body[:100] if comment.body else None
            # Get actor from comment author
            if comment.author_id:
                author = authors.get(comment.author_id)
                if author:
                    actor_name = author.name
            # A share-link guest has no account; the comment names them instead
            elif comment.guest_author_id:
                guest = guests.get(comment.guest_author_id)
                if guest:
                    actor_name = guest.name
                    actor_is_guest = True

        results.append(NotificationResponse(
            id=n.id,
            type=n.type,
            asset_id=n.asset_id,
            comment_id=n.comment_id,
            read=n.read,
            created_at=n.created_at,
            asset_name=asset_name,
            actor_name=actor_name,
            actor_is_guest=actor_is_guest,
            comment_preview=comment_preview,
            project_id=project_id,
        ))

    return results


@router.post("/me/notifications/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT)
def mark_notification_read(
    notification_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    notification = db.query(Notification).filter(
        Notification.id == notification_id,
        Notification.user_id == current_user.id,
    ).first()
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")
    notification.read = True
    db.commit()


@router.post("/me/notifications/read-all", status_code=status.HTTP_204_NO_CONTENT)
def mark_all_notifications_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db.query(Notification).filter(
        Notification.user_id == current_user.id,
        Notification.read == False,
    ).update({"read": True})
    db.commit()
