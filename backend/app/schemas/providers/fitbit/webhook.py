from pydantic import BaseModel, Field


class FitbitWebhookNotification(BaseModel):
    """One entry of a Fitbit subscription-API notification.

    Fitbit POSTs a JSON *array* of these to the subscriber endpoint whenever a
    subscribed collection changes. The notification is notify-only: it carries
    the owner and the affected date, and the actual data must be pulled back
    via the REST API.

    See: https://dev.fitbit.com/build/reference/web-api/developer-guide/using-subscriptions/
    """

    collection_type: str = Field(alias="collectionType")
    date: str  # affected date, YYYY-MM-DD
    owner_id: str = Field(alias="ownerId")  # Fitbit user id
    owner_type: str = Field(alias="ownerType")
    subscription_id: str = Field(alias="subscriptionId")
