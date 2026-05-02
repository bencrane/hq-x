"""Pure TwiML XML response builders for IVR endpoints.

All functions are side-effect-free — no DB calls, no HTTP calls.
They accept data and return TwiML XML strings.
"""

from __future__ import annotations

import re

from twilio.twiml.voice_response import VoiceResponse


def build_say_response(
    text: str,
    voice: str,
    language: str,
    pause_seconds: int | None = None,
    redirect_url: str | None = None,
    audio_url: str | None = None,
) -> str:
    response = VoiceResponse()
    if audio_url:
        response.play(audio_url)
    else:
        response.say(text, voice=voice, language=language)
    if pause_seconds is not None:
        response.pause(length=pause_seconds)
    if redirect_url is not None:
        response.redirect(redirect_url, method="POST")
    return str(response)


def build_gather_dtmf_response(
    prompt_text: str,
    action_url: str,
    voice: str,
    language: str,
    num_digits: int | None = None,
    timeout: int = 5,
    finish_on_key: str = "#",
    fallback_url: str | None = None,
    audio_url: str | None = None,
) -> str:
    response = VoiceResponse()
    gather_kwargs: dict = {
        "input": "dtmf",
        "action": action_url,
        "method": "POST",
        "timeout": timeout,
        "finish_on_key": finish_on_key,
    }
    if num_digits is not None:
        gather_kwargs["num_digits"] = num_digits
    gather = response.gather(**gather_kwargs)
    if audio_url:
        gather.play(audio_url)
    else:
        gather.say(prompt_text, voice=voice, language=language)
    if fallback_url is not None:
        response.redirect(fallback_url, method="POST")
    return str(response)


def build_gather_speech_response(
    prompt_text: str,
    action_url: str,
    voice: str,
    language: str,
    input_mode: str = "speech",
    timeout: int = 5,
    speech_timeout: int | str = "auto",
    fallback_url: str | None = None,
    audio_url: str | None = None,
) -> str:
    response = VoiceResponse()
    gather_kwargs: dict = {
        "input": input_mode,
        "action": action_url,
        "method": "POST",
        "timeout": timeout,
        "speech_timeout": str(speech_timeout),
    }
    gather = response.gather(**gather_kwargs)
    if audio_url:
        gather.play(audio_url)
    else:
        gather.say(prompt_text, voice=voice, language=language)
    if fallback_url is not None:
        response.redirect(fallback_url, method="POST")
    return str(response)


def build_data_lookup_hold_response(
    hold_message: str,
    redirect_url: str,
    voice: str,
    language: str,
    audio_url: str | None = None,
) -> str:
    response = VoiceResponse()
    if audio_url:
        response.play(audio_url)
    else:
        response.say(hold_message, voice=voice, language=language)
    response.redirect(redirect_url, method="POST")
    return str(response)


def build_dynamic_say_response(
    template_text: str,
    session_data: dict,
    voice: str,
    language: str,
    redirect_url: str | None = None,
    audio_url: str | None = None,
) -> str:
    response = VoiceResponse()
    if audio_url:
        response.play(audio_url)
    else:
        def _resolve_var(match: re.Match) -> str:
            var_path = match.group(1).strip()
            parts = var_path.split(".")
            value = session_data
            for part in parts:
                if isinstance(value, dict):
                    value = value.get(part)
                else:
                    return ""
                if value is None:
                    return ""
            return str(value)

        resolved_text = re.sub(r"\{\{(.+?)\}\}", _resolve_var, template_text)
        response.say(resolved_text, voice=voice, language=language)
    if redirect_url is not None:
        response.redirect(redirect_url, method="POST")
    return str(response)


def build_transfer_response(
    number: str,
    action_url: str,
    caller_id: str | None = None,
    timeout: int = 30,
    record: str = "do-not-record",
    recording_status_callback: str | None = None,
) -> str:
    response = VoiceResponse()
    dial_kwargs: dict = {
        "action": action_url,
        "method": "POST",
        "timeout": timeout,
        "record": record,
    }
    if caller_id is not None:
        dial_kwargs["caller_id"] = caller_id
    if recording_status_callback is not None:
        dial_kwargs["recording_status_callback"] = recording_status_callback
    dial = response.dial(**dial_kwargs)
    dial.number(number)
    return str(response)


def build_record_response(
    prompt_text: str,
    action_url: str,
    voice: str,
    language: str,
    max_length: int = 120,
    play_beep: bool = True,
    recording_status_callback: str | None = None,
    audio_url: str | None = None,
) -> str:
    response = VoiceResponse()
    if audio_url:
        response.play(audio_url)
    else:
        response.say(prompt_text, voice=voice, language=language)
    record_kwargs: dict = {
        "action": action_url,
        "method": "POST",
        "max_length": max_length,
        "play_beep": play_beep,
    }
    if recording_status_callback is not None:
        record_kwargs["recording_status_callback"] = recording_status_callback
    response.record(**record_kwargs)
    return str(response)


def build_hangup_response(
    goodbye_text: str | None = None,
    voice: str | None = None,
    language: str | None = None,
    audio_url: str | None = None,
) -> str:
    response = VoiceResponse()
    if audio_url:
        response.play(audio_url)
    elif goodbye_text is not None:
        response.say(
            goodbye_text,
            voice=voice or "Polly.Joanna-Generative",
            language=language or "en-US",
        )
    response.hangup()
    return str(response)


def build_outbound_connect_response(
    greeting_text: str | None = None,
    voice: str = "Polly.Matthew-Generative",
    language: str = "en-US",
    pause_seconds: int = 2,
    redirect_url: str | None = None,
) -> str:
    """TwiML for when an outbound call connects (before AMD result is known)."""
    response = VoiceResponse()
    if greeting_text:
        response.say(greeting_text, voice=voice, language=language)
    else:
        response.pause(length=pause_seconds)
    if redirect_url is not None:
        response.redirect(redirect_url, method="POST")
    else:
        response.hangup()
    return str(response)


def build_voicemail_drop_response(
    message_text: str | None = None,
    audio_url: str | None = None,
    voice: str = "Polly.Matthew-Generative",
    language: str = "en-US",
) -> str:
    """TwiML for dropping a message into a voicemail box."""
    if message_text and audio_url:
        raise ValueError("Provide exactly one of message_text or audio_url, not both")
    if not message_text and not audio_url:
        raise ValueError("Provide exactly one of message_text or audio_url")
    response = VoiceResponse()
    if message_text:
        response.say(message_text, voice=voice, language=language)
    else:
        response.play(audio_url)
    response.hangup()
    return str(response)


def build_human_answered_response(
    message_text: str,
    voice: str = "Polly.Matthew-Generative",
    language: str = "en-US",
) -> str:
    """TwiML for when AMD determines a human answered (Layer 2 placeholder)."""
    response = VoiceResponse()
    response.say(message_text, voice=voice, language=language)
    response.hangup()
    return str(response)


def add_live_transcription(
    response: VoiceResponse,
    *,
    status_callback_url: str,
    name: str = "live-transcript",
    track: str = "both_tracks",
    transcription_engine: str = "deepgram",
    language_code: str = "en-US",
    intelligence_service_sid: str | None = None,
    inbound_track_label: str = "customer",
    outbound_track_label: str = "agent",
) -> VoiceResponse:
    """Inject <Start><Transcription> into a VoiceResponse for live call transcription.

    Must be called before other TwiML verbs (Say, Gather, Dial) so the transcription
    starts at the beginning of the call.

    Returns the same VoiceResponse for chaining.
    """
    start = response.start()
    kwargs: dict = {
        "name": name,
        "track": track,
        "status_callback_url": status_callback_url,
        "status_callback_method": "POST",
        "transcription_engine": transcription_engine,
        "language_code": language_code,
        "inbound_track_label": inbound_track_label,
        "outbound_track_label": outbound_track_label,
        "enable_automatic_punctuation": "true",
    }
    if intelligence_service_sid:
        kwargs["intelligence_service"] = intelligence_service_sid
    start.transcription(**kwargs)
    return response


def build_outbound_connect_response_with_transcription(
    *,
    status_callback_url: str,
    transcription_engine: str = "deepgram",
    language_code: str = "en-US",
    intelligence_service_sid: str | None = None,
    greeting_text: str | None = None,
    voice: str = "Polly.Matthew-Generative",
    language: str = "en-US",
    pause_seconds: int = 2,
    redirect_url: str | None = None,
) -> str:
    """Build outbound connect TwiML with live transcription included."""
    response = VoiceResponse()
    add_live_transcription(
        response,
        status_callback_url=status_callback_url,
        transcription_engine=transcription_engine,
        language_code=language_code,
        intelligence_service_sid=intelligence_service_sid,
    )
    if greeting_text:
        response.say(greeting_text, voice=voice, language=language)
    else:
        response.pause(length=pause_seconds)
    if redirect_url is not None:
        response.redirect(redirect_url, method="POST")
    else:
        response.hangup()
    return str(response)


def build_vapi_sip_transfer_response(
    sip_uri: str,
    caller_id: str,
    sip_headers: dict[str, str] | None = None,
) -> str:
    """TwiML for forwarding a call to Vapi via SIP with assistant context in headers."""
    response = VoiceResponse()
    dial = response.dial(caller_id=caller_id)
    sip_uri_str = sip_uri
    if sip_headers:
        header_str = "&".join(f"{k}={v}" for k, v in sip_headers.items())
        sip_uri_str = f"{sip_uri}?{header_str}"
    dial.sip(sip_uri_str)
    return str(response)


def build_error_response() -> str:
    response = VoiceResponse()
    response.say(
        "We're sorry, we're experiencing technical difficulties. Please try again later.",
        voice="Polly.Joanna-Generative",
        language="en-US",
    )
    response.hangup()
    return str(response)
