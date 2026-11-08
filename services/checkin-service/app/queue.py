"""SQS queue careroute-checkins: one checkin.json per message, read by the Coordinator."""
import json

from .models import Checkin


class CheckinQueue:
    def __init__(self, sqs_client, queue_name: str):
        self.sqs = sqs_client
        self.queue_name = queue_name
        self._url: str | None = None

    @property
    def url(self) -> str:
        if self._url is None:
            self._url = self.sqs.get_queue_url(QueueName=self.queue_name)["QueueUrl"]
        return self._url

    def send(self, checkin: Checkin) -> None:
        self.sqs.send_message(
            QueueUrl=self.url,
            MessageBody=json.dumps(checkin.to_contract()),
            # Lets the Coordinator filter or log without parsing the body.
            MessageAttributes={
                "tripId": {"DataType": "String", "StringValue": checkin.tripId},
                "bufferedOffline": {"DataType": "String", "StringValue": str(checkin.bufferedOffline).lower()},
            },
        )

    def ping(self) -> None:
        _ = self.url
