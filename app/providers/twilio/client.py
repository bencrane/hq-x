from __future__ import annotations

from typing import Any

from app.providers.twilio._http import (
    TwilioProviderError,
    request_json,
    request_no_content,
)


TWILIO_API_BASE = "https://api.twilio.com"

# Voice / Calls
_EP_CALLS = "/2010-04-01/Accounts/{account_sid}/Calls.json"
_EP_CALL = "/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"

# Recordings
_EP_RECORDINGS = "/2010-04-01/Accounts/{account_sid}/Recordings.json"
_EP_RECORDING = "/2010-04-01/Accounts/{account_sid}/Recordings/{recording_sid}.json"

# Phone Numbers
_EP_AVAILABLE_LOCAL = "/2010-04-01/Accounts/{account_sid}/AvailablePhoneNumbers/{country_code}/Local.json"
_EP_AVAILABLE_TOLL_FREE = "/2010-04-01/Accounts/{account_sid}/AvailablePhoneNumbers/{country_code}/TollFree.json"
_EP_INCOMING_PHONE_NUMBERS = "/2010-04-01/Accounts/{account_sid}/IncomingPhoneNumbers.json"
_EP_INCOMING_PHONE_NUMBER = "/2010-04-01/Accounts/{account_sid}/IncomingPhoneNumbers/{phone_number_sid}.json"

# Addresses
_EP_ADDRESSES = "/2010-04-01/Accounts/{account_sid}/Addresses.json"

# TwiML Applications
_EP_APPLICATIONS = "/2010-04-01/Accounts/{account_sid}/Applications.json"
_EP_APPLICATION = "/2010-04-01/Accounts/{account_sid}/Applications/{application_sid}.json"

# Messaging
_EP_MESSAGES = "/2010-04-01/Accounts/{account_sid}/Messages.json"
_EP_MESSAGE = "/2010-04-01/Accounts/{account_sid}/Messages/{message_sid}.json"

# Account (for credential validation)
_EP_ACCOUNT = "/2010-04-01/Accounts/{account_sid}.json"


def create_address(
    account_sid: str,
    auth_token: str,
    *,
    customer_name: str,
    street: str,
    city: str,
    region: str,
    postal_code: str,
    iso_country: str,
    street_secondary: str | None = None,
    friendly_name: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Create a Twilio Address resource (used for Trust Hub SupportingDocuments)."""
    url = f"{TWILIO_API_BASE}{_EP_ADDRESSES.format(account_sid=account_sid)}"
    form: dict[str, str] = {
        "CustomerName": customer_name,
        "Street": street,
        "City": city,
        "Region": region,
        "PostalCode": postal_code,
        "IsoCountry": iso_country,
    }
    if street_secondary is not None:
        form["StreetSecondary"] = street_secondary
    if friendly_name is not None:
        form["FriendlyName"] = friendly_name

    return request_json(
        method="POST",
        url=url,
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        data=form,
    )


def create_call(
    account_sid: str,
    auth_token: str,
    *,
    to: str,
    from_number: str,
    url: str | None = None,
    twiml: str | None = None,
    application_sid: str | None = None,
    status_callback: str | None = None,
    status_callback_event: list[str] | None = None,
    machine_detection: str | None = None,
    async_amd: bool | None = None,
    async_amd_status_callback: str | None = None,
    record: bool = False,
    recording_status_callback: str | None = None,
    timeout: int = 30,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Create an outbound voice call via Twilio REST API."""
    provided = sum(1 for x in (url, twiml, application_sid) if x is not None)
    if provided != 1:
        raise TwilioProviderError(
            "Twilio bad request: exactly one of url, twiml, or application_sid must be provided"
        )

    form: dict[str, Any] = {
        "To": to,
        "From": from_number,
        "Timeout": str(timeout),
    }
    if url is not None:
        form["Url"] = url
    if twiml is not None:
        form["Twiml"] = twiml
    if application_sid is not None:
        form["ApplicationSid"] = application_sid
    if status_callback is not None:
        form["StatusCallback"] = status_callback
    if status_callback_event is not None:
        form["StatusCallbackEvent"] = " ".join(status_callback_event)
    if machine_detection is not None:
        form["MachineDetection"] = machine_detection
    if async_amd is not None:
        form["AsyncAmd"] = "true" if async_amd else "false"
    if async_amd_status_callback is not None:
        form["AsyncAmdStatusCallback"] = async_amd_status_callback
    if record:
        form["Record"] = "true"
    if recording_status_callback is not None:
        form["RecordingStatusCallback"] = recording_status_callback

    endpoint = _EP_CALLS.format(account_sid=account_sid)
    return request_json(
        method="POST",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        data=form,
    )


def get_call(
    account_sid: str,
    auth_token: str,
    *,
    call_sid: str,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    endpoint = _EP_CALL.format(account_sid=account_sid, call_sid=call_sid)
    return request_json(
        method="GET",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
    )


def update_call(
    account_sid: str,
    auth_token: str,
    *,
    call_sid: str,
    url: str | None = None,
    twiml: str | None = None,
    status: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Update an in-progress call. Use status='completed' to hang up."""
    form: dict[str, Any] = {}
    if url is not None:
        form["Url"] = url
    if twiml is not None:
        form["Twiml"] = twiml
    if status is not None:
        form["Status"] = status

    endpoint = _EP_CALL.format(account_sid=account_sid, call_sid=call_sid)
    return request_json(
        method="POST",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        data=form,
    )


def list_recordings(
    account_sid: str,
    auth_token: str,
    *,
    call_sid: str | None = None,
    date_created: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if call_sid is not None:
        params["CallSid"] = call_sid
    if date_created is not None:
        params["DateCreated"] = date_created

    endpoint = _EP_RECORDINGS.format(account_sid=account_sid)
    return request_json(
        method="GET",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        params=params if params else None,
    )


def get_recording(
    account_sid: str,
    auth_token: str,
    *,
    recording_sid: str,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    endpoint = _EP_RECORDING.format(account_sid=account_sid, recording_sid=recording_sid)
    return request_json(
        method="GET",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
    )


def delete_recording(
    account_sid: str,
    auth_token: str,
    *,
    recording_sid: str,
    timeout_seconds: float = 10.0,
) -> None:
    endpoint = _EP_RECORDING.format(account_sid=account_sid, recording_sid=recording_sid)
    request_no_content(
        method="DELETE",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
    )


def search_available_numbers(
    account_sid: str,
    auth_token: str,
    *,
    country_code: str = "US",
    number_type: str = "Local",
    area_code: str | None = None,
    in_region: str | None = None,
    in_postal_code: str | None = None,
    contains: str | None = None,
    sms_enabled: bool | None = None,
    voice_enabled: bool | None = None,
    limit: int = 20,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Search for available phone numbers to purchase."""
    if number_type == "TollFree":
        ep_template = _EP_AVAILABLE_TOLL_FREE
    else:
        ep_template = _EP_AVAILABLE_LOCAL

    endpoint = ep_template.format(account_sid=account_sid, country_code=country_code)
    params: dict[str, Any] = {"limit": limit}
    if area_code is not None:
        params["AreaCode"] = area_code
    if in_region is not None:
        params["InRegion"] = in_region
    if in_postal_code is not None:
        params["InPostalCode"] = in_postal_code
    if contains is not None:
        params["Contains"] = contains
    if sms_enabled is not None:
        params["SmsEnabled"] = str(sms_enabled).lower()
    if voice_enabled is not None:
        params["VoiceEnabled"] = str(voice_enabled).lower()

    return request_json(
        method="GET",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        params=params,
    )


def purchase_phone_number(
    account_sid: str,
    auth_token: str,
    *,
    phone_number: str,
    voice_application_sid: str | None = None,
    voice_url: str | None = None,
    sms_url: str | None = None,
    status_callback: str | None = None,
    friendly_name: str | None = None,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    form: dict[str, Any] = {"PhoneNumber": phone_number}
    if voice_application_sid is not None:
        form["VoiceApplicationSid"] = voice_application_sid
    if voice_url is not None:
        form["VoiceUrl"] = voice_url
    if sms_url is not None:
        form["SmsUrl"] = sms_url
    if status_callback is not None:
        form["StatusCallback"] = status_callback
    if friendly_name is not None:
        form["FriendlyName"] = friendly_name

    endpoint = _EP_INCOMING_PHONE_NUMBERS.format(account_sid=account_sid)
    return request_json(
        method="POST",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        data=form,
    )


def get_phone_number(
    account_sid: str,
    auth_token: str,
    *,
    phone_number_sid: str,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    endpoint = _EP_INCOMING_PHONE_NUMBER.format(
        account_sid=account_sid, phone_number_sid=phone_number_sid
    )
    return request_json(
        method="GET",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
    )


def update_phone_number(
    account_sid: str,
    auth_token: str,
    *,
    phone_number_sid: str,
    voice_application_sid: str | None = None,
    voice_url: str | None = None,
    sms_url: str | None = None,
    friendly_name: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    form: dict[str, Any] = {}
    if voice_application_sid is not None:
        form["VoiceApplicationSid"] = voice_application_sid
    if voice_url is not None:
        form["VoiceUrl"] = voice_url
    if sms_url is not None:
        form["SmsUrl"] = sms_url
    if friendly_name is not None:
        form["FriendlyName"] = friendly_name

    endpoint = _EP_INCOMING_PHONE_NUMBER.format(
        account_sid=account_sid, phone_number_sid=phone_number_sid
    )
    return request_json(
        method="POST",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        data=form,
    )


def list_phone_numbers(
    account_sid: str,
    auth_token: str,
    *,
    phone_number: str | None = None,
    friendly_name: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if phone_number is not None:
        params["PhoneNumber"] = phone_number
    if friendly_name is not None:
        params["FriendlyName"] = friendly_name

    endpoint = _EP_INCOMING_PHONE_NUMBERS.format(account_sid=account_sid)
    return request_json(
        method="GET",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        params=params if params else None,
    )


def release_phone_number(
    account_sid: str,
    auth_token: str,
    *,
    phone_number_sid: str,
    timeout_seconds: float = 10.0,
) -> None:
    endpoint = _EP_INCOMING_PHONE_NUMBER.format(
        account_sid=account_sid, phone_number_sid=phone_number_sid
    )
    request_no_content(
        method="DELETE",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
    )


def validate_credentials(
    account_sid: str,
    auth_token: str,
    *,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    endpoint = _EP_ACCOUNT.format(account_sid=account_sid)
    return request_json(
        method="GET",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------


def send_message(
    account_sid: str,
    auth_token: str,
    *,
    to: str,
    body: str | None = None,
    from_number: str | None = None,
    messaging_service_sid: str | None = None,
    media_url: list[str] | None = None,
    status_callback: str | None = None,
    validity_period: int | None = None,
    schedule_type: str | None = None,
    send_at: str | None = None,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """Send an SMS or MMS message via Twilio REST API."""
    if from_number and messaging_service_sid:
        raise TwilioProviderError(
            "Twilio bad request: cannot provide both from_number and messaging_service_sid"
        )
    if not from_number and not messaging_service_sid:
        raise TwilioProviderError(
            "Twilio bad request: must provide either from_number or messaging_service_sid"
        )
    if not body and not media_url:
        raise TwilioProviderError(
            "Twilio bad request: must provide at least one of body or media_url"
        )
    if (schedule_type or send_at) and not messaging_service_sid:
        raise TwilioProviderError(
            "Twilio bad request: scheduling requires messaging_service_sid"
        )

    form_pairs: list[tuple[str, str]] = [("To", to)]
    if from_number is not None:
        form_pairs.append(("From", from_number))
    if messaging_service_sid is not None:
        form_pairs.append(("MessagingServiceSid", messaging_service_sid))
    if body is not None:
        form_pairs.append(("Body", body))
    if media_url:
        for url in media_url:
            form_pairs.append(("MediaUrl", url))
    if status_callback is not None:
        form_pairs.append(("StatusCallback", status_callback))
    if validity_period is not None:
        form_pairs.append(("ValidityPeriod", str(validity_period)))
    if schedule_type is not None:
        form_pairs.append(("ScheduleType", schedule_type))
    if send_at is not None:
        form_pairs.append(("SendAt", send_at))

    endpoint = _EP_MESSAGES.format(account_sid=account_sid)
    return request_json(
        method="POST",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        data=form_pairs,
    )


def get_message(
    account_sid: str,
    auth_token: str,
    *,
    message_sid: str,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    endpoint = _EP_MESSAGE.format(account_sid=account_sid, message_sid=message_sid)
    return request_json(
        method="GET",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
    )


def list_messages(
    account_sid: str,
    auth_token: str,
    *,
    to: str | None = None,
    from_number: str | None = None,
    date_sent: str | None = None,
    date_sent_after: str | None = None,
    date_sent_before: str | None = None,
    page_size: int = 50,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    params: dict[str, Any] = {"PageSize": page_size}
    if to is not None:
        params["To"] = to
    if from_number is not None:
        params["From"] = from_number
    if date_sent is not None:
        params["DateSent"] = date_sent
    if date_sent_after is not None:
        params["DateSent>"] = date_sent_after
    if date_sent_before is not None:
        params["DateSent<"] = date_sent_before

    endpoint = _EP_MESSAGES.format(account_sid=account_sid)
    return request_json(
        method="GET",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        params=params,
    )


def cancel_scheduled_message(
    account_sid: str,
    auth_token: str,
    *,
    message_sid: str,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    endpoint = _EP_MESSAGE.format(account_sid=account_sid, message_sid=message_sid)
    return request_json(
        method="POST",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        data={"Status": "canceled"},
    )


# ---------------------------------------------------------------------------
# TwiML Applications
# ---------------------------------------------------------------------------


def create_application(
    account_sid: str,
    auth_token: str,
    *,
    friendly_name: str,
    voice_url: str | None = None,
    voice_method: str | None = None,
    voice_fallback_url: str | None = None,
    status_callback: str | None = None,
    status_callback_method: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    form: dict[str, str] = {"FriendlyName": friendly_name}
    if voice_url is not None:
        form["VoiceUrl"] = voice_url
    if voice_method is not None:
        form["VoiceMethod"] = voice_method
    if voice_fallback_url is not None:
        form["VoiceFallbackUrl"] = voice_fallback_url
    if status_callback is not None:
        form["StatusCallback"] = status_callback
    if status_callback_method is not None:
        form["StatusCallbackMethod"] = status_callback_method

    endpoint = _EP_APPLICATIONS.format(account_sid=account_sid)
    return request_json(
        method="POST",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        data=form,
    )


def get_application(
    account_sid: str,
    auth_token: str,
    *,
    application_sid: str,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    endpoint = _EP_APPLICATION.format(
        account_sid=account_sid, application_sid=application_sid
    )
    return request_json(
        method="GET",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
    )


def update_application(
    account_sid: str,
    auth_token: str,
    *,
    application_sid: str,
    friendly_name: str | None = None,
    voice_url: str | None = None,
    voice_method: str | None = None,
    voice_fallback_url: str | None = None,
    status_callback: str | None = None,
    status_callback_method: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    form: dict[str, str] = {}
    if friendly_name is not None:
        form["FriendlyName"] = friendly_name
    if voice_url is not None:
        form["VoiceUrl"] = voice_url
    if voice_method is not None:
        form["VoiceMethod"] = voice_method
    if voice_fallback_url is not None:
        form["VoiceFallbackUrl"] = voice_fallback_url
    if status_callback is not None:
        form["StatusCallback"] = status_callback
    if status_callback_method is not None:
        form["StatusCallbackMethod"] = status_callback_method

    endpoint = _EP_APPLICATION.format(
        account_sid=account_sid, application_sid=application_sid
    )
    return request_json(
        method="POST",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        data=form,
    )


def list_applications(
    account_sid: str,
    auth_token: str,
    *,
    friendly_name: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if friendly_name is not None:
        params["FriendlyName"] = friendly_name

    endpoint = _EP_APPLICATIONS.format(account_sid=account_sid)
    return request_json(
        method="GET",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
        params=params if params else None,
    )


def delete_application(
    account_sid: str,
    auth_token: str,
    *,
    application_sid: str,
    timeout_seconds: float = 10.0,
) -> None:
    endpoint = _EP_APPLICATION.format(
        account_sid=account_sid, application_sid=application_sid
    )
    request_no_content(
        method="DELETE",
        url=f"{TWILIO_API_BASE}{endpoint}",
        account_sid=account_sid,
        auth_token=auth_token,
        timeout_seconds=timeout_seconds,
    )
