from django.http import HttpRequest, HttpResponse
from pydantic import Json

from zerver.actions.unsubscribed_mentions import do_notify_unsubscribed_mentioned_users
from zerver.lib.message import access_message
from zerver.lib.response import json_success
from zerver.lib.typed_endpoint import typed_endpoint
from zerver.models import UserProfile


@typed_endpoint
def notify_unsubscribed_mentioned_users_backend(
    request: HttpRequest,
    user_profile: UserProfile,
    message_id: int,
    *,
    user_ids: Json[list[int]],
) -> HttpResponse:
    message = access_message(user_profile, message_id, is_modifying_message=False)
    do_notify_unsubscribed_mentioned_users(user_profile, message, user_ids)
    return json_success(request)
