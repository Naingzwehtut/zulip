from typing import TYPE_CHECKING

import orjson
from django.conf import settings
from typing_extensions import override

from zerver.actions.streams import do_change_stream_group_based_setting
from zerver.actions.unsubscribed_mentions import get_directly_mentioned_user_ids
from zerver.actions.user_groups import check_add_user_group
from zerver.actions.user_settings import do_change_user_setting
from zerver.actions.users import do_deactivate_user
from zerver.lib.test_classes import ZulipTestCase
from zerver.lib.test_helpers import get_subscription, most_recent_message
from zerver.lib.topic_link_util import get_message_link_syntax
from zerver.models import Message, Stream, Subscription, UserProfile
from zerver.models.users import get_system_bot

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse as TestHttpResponse


class NotifyUnsubscribedMentionsTest(ZulipTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.sender = self.example_user("iago")
        self.hamlet = self.example_user("hamlet")
        self.othello = self.example_user("othello")
        self.channel = self.make_stream("mentions channel")
        self.subscribe(self.sender, self.channel.name)
        self.notification_bot_user = get_system_bot(settings.NOTIFICATION_BOT, self.sender.realm_id)

    def send_mention(self, content: str, stream: Stream | None = None) -> int:
        if stream is None:
            stream = self.channel
        return self.send_stream_message(
            self.sender, stream.name, content=content, topic_name="planning"
        )

    def notify(
        self, message_id: int, user_ids: list[int], sender: UserProfile | None = None
    ) -> "TestHttpResponse":
        return self.api_post(
            self.sender if sender is None else sender,
            f"/api/v1/messages/{message_id}/notify_unsubscribed_mentions",
            {"user_ids": orjson.dumps(user_ids).decode()},
        )

    def count_notification_bot_messages(self) -> int:
        return Message.objects.filter(
            realm_id=self.sender.realm_id, sender=self.notification_bot_user
        ).count()

    def test_get_directly_mentioned_user_ids(self) -> None:
        hamlet_id = self.hamlet.id
        othello_id = self.othello.id
        rendered = (
            f'<p><span class="user-mention" data-user-id="{hamlet_id}">@King Hamlet</span> '
            f'<span class="user-mention silent" data-user-id="{othello_id}">Othello</span> '
            '<span class="user-mention channel-wildcard-mention" data-user-id="*">@all</span> '
            '<span class="user-group-mention" data-user-group-id="1">@group</span></p>'
        )
        self.assertEqual(get_directly_mentioned_user_ids(rendered), {hamlet_id})
        self.assertEqual(get_directly_mentioned_user_ids(""), set())
        self.assertEqual(get_directly_mentioned_user_ids(None), set())

    def test_notify_success(self) -> None:
        content = f"@**|{self.hamlet.id}** could you review this?"
        message_id = self.send_mention(content)

        result = self.notify(message_id, [self.hamlet.id])
        self.assert_json_success(result)

        notification = most_recent_message(self.hamlet)
        self.assertEqual(notification.sender_id, self.notification_bot_user.id)
        message_link = get_message_link_syntax(
            self.channel.id, self.channel.name, "planning", message_id
        )
        expected_content = (
            f"@_**Iago|{self.sender.id}** mentioned you in a channel you are not "
            f"subscribed to: {message_link}\n\n"
            f"```quote\n{content}\n```"
        )
        self.assertEqual(notification.content, expected_content)

        # The mentioned user is not subscribed as a side effect.
        with self.assertRaises(Subscription.DoesNotExist):
            get_subscription(self.channel.name, self.hamlet)

    def test_notify_uses_recipient_language(self) -> None:
        do_change_user_setting(self.hamlet, "default_language", "de", acting_user=None)
        message_id = self.send_mention(f"@**|{self.hamlet.id}** hello")
        self.assert_json_success(self.notify(message_id, [self.hamlet.id]))
        # We don't depend on the translation being present; we just
        # check that the notification is sent with the expected link.
        notification = most_recent_message(self.hamlet)
        self.assertIn(f"@{message_id}**", notification.content)

    def test_notify_truncates_long_messages(self) -> None:
        long_content = f"@**|{self.hamlet.id}** " + "a" * settings.MAX_MESSAGE_LENGTH
        message_id = self.send_mention(long_content)
        self.assert_json_success(self.notify(message_id, [self.hamlet.id]))
        notification = most_recent_message(self.hamlet)
        self.assertIn("[message truncated]", notification.content)
        self.assertLessEqual(len(notification.content), settings.MAX_MESSAGE_LENGTH)

    def test_only_sender_can_notify(self) -> None:
        message_id = self.send_mention(f"@**|{self.hamlet.id}** hello")
        self.subscribe(self.othello, self.channel.name)
        result = self.notify(message_id, [self.hamlet.id], sender=self.othello)
        self.assert_json_error(result, "You can only notify users mentioned in your own messages.")

    def test_direct_messages_not_allowed(self) -> None:
        message_id = self.send_personal_message(
            self.sender, self.othello, f"@**|{self.hamlet.id}** hello"
        )
        result = self.notify(message_id, [self.hamlet.id])
        self.assert_json_error(result, "Only users mentioned in channel messages can be notified.")

    def test_user_must_be_mentioned(self) -> None:
        # Not mentioned at all.
        message_id = self.send_mention("no mentions here")
        result = self.notify(message_id, [self.hamlet.id])
        self.assert_json_error(result, f"User {self.hamlet.id} is not mentioned in this message.")

        # Silent mentions don't count.
        message_id = self.send_mention(f"@_**|{self.hamlet.id}** hello")
        result = self.notify(message_id, [self.hamlet.id])
        self.assert_json_error(result, f"User {self.hamlet.id} is not mentioned in this message.")

    def test_subscribed_user(self) -> None:
        self.subscribe(self.hamlet, self.channel.name)
        message_id = self.send_mention(f"@**|{self.hamlet.id}** hello")
        result = self.notify(message_id, [self.hamlet.id])
        self.assert_json_error(
            result, f"User {self.hamlet.id} is already subscribed to this channel."
        )

    def test_invalid_users(self) -> None:
        message_id = self.send_mention(f"@**|{self.hamlet.id}** hello")
        result = self.notify(message_id, [9999])
        self.assert_json_error(result, "Invalid user ID: 9999")

        do_deactivate_user(self.hamlet, acting_user=None)
        result = self.notify(message_id, [self.hamlet.id])
        self.assert_json_error(result, f"Invalid user ID: {self.hamlet.id}")

    def test_guest_user(self) -> None:
        polonius = self.example_user("polonius")
        message_id = self.send_mention(f"@**|{polonius.id}** hello")
        result = self.notify(message_id, [polonius.id])
        self.assert_json_error(
            result, f"User {polonius.id} cannot be notified about messages in this channel."
        )

        # Guests can be notified about messages in web-public channels.
        web_public_channel = self.make_stream("web public mentions", is_web_public=True)
        self.subscribe(self.sender, web_public_channel.name)
        message_id = self.send_mention(f"@**|{polonius.id}** hello", stream=web_public_channel)
        self.assert_json_success(self.notify(message_id, [polonius.id]))

    def test_private_channel(self) -> None:
        private_channel = self.make_stream(
            "private mentions", invite_only=True, history_public_to_subscribers=True
        )
        self.subscribe(self.sender, private_channel.name)
        message_id = self.send_mention(f"@**|{self.hamlet.id}** hello", stream=private_channel)

        # Hamlet doesn't have permission to join the channel.
        result = self.notify(message_id, [self.hamlet.id])
        self.assert_json_error(
            result, f"User {self.hamlet.id} cannot be notified about messages in this channel."
        )

        # Once Hamlet is allowed to subscribe, he can be notified.
        hamlet_group = check_add_user_group(
            self.sender.realm, "hamlet group", [self.hamlet], acting_user=self.sender
        )
        do_change_stream_group_based_setting(
            private_channel, "can_subscribe_group", hamlet_group, acting_user=self.sender
        )
        self.assert_json_success(self.notify(message_id, [self.hamlet.id]))

        # With protected history, Hamlet couldn't read the message
        # after subscribing, so we don't notify him.
        protected_channel = self.make_stream(
            "protected mentions", invite_only=True, history_public_to_subscribers=False
        )
        self.subscribe(self.sender, protected_channel.name)
        do_change_stream_group_based_setting(
            protected_channel, "can_subscribe_group", hamlet_group, acting_user=self.sender
        )
        message_id = self.send_mention(f"@**|{self.hamlet.id}** hello", stream=protected_channel)
        result = self.notify(message_id, [self.hamlet.id])
        self.assert_json_error(
            result, f"User {self.hamlet.id} cannot be notified about messages in this channel."
        )

    def test_no_partial_notifications(self) -> None:
        self.subscribe(self.othello, self.channel.name)
        message_id = self.send_mention(f"@**|{self.hamlet.id}** and @**|{self.othello.id}** hello")
        count = self.count_notification_bot_messages()
        result = self.notify(message_id, [self.hamlet.id, self.othello.id])
        self.assert_json_error(
            result, f"User {self.othello.id} is already subscribed to this channel."
        )
        self.assertEqual(self.count_notification_bot_messages(), count)

    def test_notify_multiple_users(self) -> None:
        cordelia = self.example_user("cordelia")
        message_id = self.send_mention(f"@**|{self.hamlet.id}** and @**|{cordelia.id}** hello")
        count = self.count_notification_bot_messages()
        self.assert_json_success(self.notify(message_id, [self.hamlet.id, cordelia.id]))
        self.assertEqual(self.count_notification_bot_messages(), count + 2)
        for user in [self.hamlet, cordelia]:
            self.assertEqual(most_recent_message(user).sender_id, self.notification_bot_user.id)
