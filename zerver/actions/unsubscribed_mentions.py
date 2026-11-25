import lxml.html
from django.conf import settings
from django.utils.translation import gettext as _
from django.utils.translation import override as override_language

from zerver.actions.message_send import internal_send_private_message
from zerver.lib.exceptions import JsonableError
from zerver.lib.markdown.fenced_code import wrap_in_quote_block
from zerver.lib.mention import silent_mention_syntax_for_user
from zerver.lib.message import truncate_content
from zerver.lib.streams import user_has_content_access
from zerver.lib.topic_link_util import get_message_link_syntax
from zerver.lib.user_groups import UserGroupMembershipDetails
from zerver.lib.users import user_ids_to_users
from zerver.models import Message, Stream, Subscription, UserProfile
from zerver.models.users import get_system_bot

# Leave room for the header line above the quoted message.
MAX_QUOTED_MESSAGE_LENGTH = settings.MAX_MESSAGE_LENGTH - 500


def get_directly_mentioned_user_ids(rendered_content: str | None) -> set[int]:
    """Returns the IDs of users with a non-silent personal mention in
    the rendered message. Mentions inside quotes are rendered as
    silent mentions, so they are excluded as well."""
    if not rendered_content:
        return set()
    fragment = lxml.html.fragment_fromstring(rendered_content, create_parent=True)
    user_ids: set[int] = set()
    for element in fragment.iterfind(".//span[@data-user-id]"):
        classes = element.get("class", "").split()
        if "user-mention" not in classes or "silent" in classes:
            continue
        user_id = element.get("data-user-id", "")
        if user_id.isdecimal():
            user_ids.add(int(user_id))
    return user_ids


def can_notify_unsubscribed_user(user_profile: UserProfile, stream: Stream) -> bool:
    # We only notify users who could subscribe to the channel and then
    # read the message, so that the notification never shares content
    # the user couldn't otherwise access. For channels with protected
    # history, a newly subscribed user can't see older messages.
    return stream.is_history_public_to_subscribers() and user_has_content_access(
        user_profile,
        stream,
        UserGroupMembershipDetails(user_recursive_group_ids=None),
        is_subscribed=False,
    )


def do_notify_unsubscribed_mentioned_users(
    sender: UserProfile, message: Message, user_ids: list[int]
) -> None:
    if message.sender_id != sender.id:
        raise JsonableError(_("You can only notify users mentioned in your own messages."))

    if not message.is_channel_message:
        raise JsonableError(_("Only users mentioned in channel messages can be notified."))

    stream = Stream.objects.get(id=message.recipient.type_id)
    users = user_ids_to_users(user_ids, sender.realm, allow_deactivated=False, allow_bots=False)

    mentioned_user_ids = get_directly_mentioned_user_ids(message.rendered_content)
    subscribed_user_ids = set(
        Subscription.objects.filter(
            recipient_id=message.recipient_id,
            user_profile_id__in=user_ids,
            active=True,
        ).values_list("user_profile_id", flat=True)
    )

    # Validate every user before sending any notification, so that
    # an invalid request doesn't result in a partial set of
    # notifications.
    for user in users:
        if user.id not in mentioned_user_ids:
            raise JsonableError(
                _("User {user_id} is not mentioned in this message.").format(user_id=user.id)
            )
        if user.id in subscribed_user_ids:
            raise JsonableError(
                _("User {user_id} is already subscribed to this channel.").format(user_id=user.id)
            )
        if not can_notify_unsubscribed_user(user, stream):
            raise JsonableError(
                _("User {user_id} cannot be notified about messages in this channel.").format(
                    user_id=user.id
                )
            )

    notification_bot = get_system_bot(settings.NOTIFICATION_BOT, sender.realm_id)
    message_link = get_message_link_syntax(stream.id, stream.name, message.topic_name(), message.id)
    sender_mention = silent_mention_syntax_for_user(sender)

    for user in users:
        with override_language(user.default_language):
            header = _(
                "{sender_mention} mentioned you in a channel you are not subscribed to: {message_link}"
            ).format(sender_mention=sender_mention, message_link=message_link)
            quoted_content = truncate_content(
                message.content, MAX_QUOTED_MESSAGE_LENGTH, "\n" + _("[message truncated]")
            )
        quote_block = wrap_in_quote_block(
            quoted_content, default_language=sender.realm.default_code_block_language
        )
        internal_send_private_message(
            notification_bot, user, f"{header}\n\n{quote_block}", acting_user=sender
        )
