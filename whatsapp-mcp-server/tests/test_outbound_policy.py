"""Tests for the opt-in read-only mode and recipient allowlist."""

import pytest
from mcp.server.fastmcp import FastMCP

import outbound_policy
import whatsapp


def _no_map(value: str) -> list[str]:
    """Aliases with no LID mapping: bare plus both suffixes (mirrors whatsapp._sender_aliases)."""
    return [value, f"{value}@s.whatsapp.net", f"{value}@lid"]


def _mapped(value: str) -> list[str]:
    """Phone 12025551234 <-> LID 99887766 resolve to each other."""
    if value in ("12025551234", "99887766"):
        return ["12025551234", "12025551234@s.whatsapp.net", "99887766", "99887766@lid"]
    return _no_map(value)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv(outbound_policy.READ_ONLY_ENV, raising=False)
    monkeypatch.delenv(outbound_policy.ALLOWED_RECIPIENTS_ENV, raising=False)


class TestDefaults:
    def test_nothing_configured_allows_everything(self):
        assert outbound_policy.denial_reason("12025551234", _no_map) is None
        assert outbound_policy.denial_reason("123456@g.us", _no_map) is None

    @pytest.mark.parametrize("value", ["", "   ", "\n"])
    def test_blank_allowlist_means_no_allowlist(self, monkeypatch, value):
        monkeypatch.setenv(outbound_policy.ALLOWED_RECIPIENTS_ENV, value)
        assert outbound_policy.denial_reason("12025551234", _no_map) is None


class TestReadOnly:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " True "])
    def test_truthy_values_block_everything(self, monkeypatch, value):
        monkeypatch.setenv(outbound_policy.READ_ONLY_ENV, value)
        assert "Read-only" in outbound_policy.denial_reason("12025551234", _no_map)

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe"])
    def test_other_values_do_not(self, monkeypatch, value):
        monkeypatch.setenv(outbound_policy.READ_ONLY_ENV, value)
        assert outbound_policy.denial_reason("12025551234", _no_map) is None

    def test_read_only_beats_allowlist(self, monkeypatch):
        monkeypatch.setenv(outbound_policy.READ_ONLY_ENV, "true")
        monkeypatch.setenv(outbound_policy.ALLOWED_RECIPIENTS_ENV, "12025551234")
        assert "Read-only" in outbound_policy.denial_reason("12025551234", _no_map)


class TestAllowlist:
    @pytest.mark.parametrize(
        "target",
        ["12025551234", "+12025551234", "12025551234@s.whatsapp.net", "+1 (202) 555-1234", "12025551234@LID"],
    )
    def test_listed_person_matches_in_any_spelling(self, monkeypatch, target):
        monkeypatch.setenv(outbound_policy.ALLOWED_RECIPIENTS_ENV, "+12025551234")
        assert outbound_policy.denial_reason(target, _no_map) is None

    @pytest.mark.parametrize("target", ["12025559999", "999@s.whatsapp.net", "123456@g.us", "", "@@", "abc"])
    def test_unlisted_is_refused(self, monkeypatch, target):
        monkeypatch.setenv(outbound_policy.ALLOWED_RECIPIENTS_ENV, "12025551234")
        assert "not in WHATSAPP_ALLOWED_RECIPIENTS" in outbound_policy.denial_reason(target, _no_map)

    def test_prefix_of_listed_number_is_not_a_match(self, monkeypatch):
        monkeypatch.setenv(outbound_policy.ALLOWED_RECIPIENTS_ENV, "12025551234")
        assert outbound_policy.denial_reason("1202555123", _no_map) is not None
        assert outbound_policy.denial_reason("120255512345", _no_map) is not None

    def test_comma_and_whitespace_separated_entries(self, monkeypatch):
        monkeypatch.setenv(outbound_policy.ALLOWED_RECIPIENTS_ENV, " 111 , 222\n333 ")
        for number in ("111", "222", "333"):
            assert outbound_policy.denial_reason(number, _no_map) is None
        assert outbound_policy.denial_reason("444", _no_map) is not None

    def test_lid_target_matches_listed_phone_via_map(self, monkeypatch):
        monkeypatch.setenv(outbound_policy.ALLOWED_RECIPIENTS_ENV, "12025551234")
        assert outbound_policy.denial_reason("99887766@lid", _mapped) is None
        assert outbound_policy.denial_reason("99887766", _mapped) is None

    def test_lid_target_without_map_is_refused(self, monkeypatch):
        monkeypatch.setenv(outbound_policy.ALLOWED_RECIPIENTS_ENV, "12025551234")
        assert outbound_policy.denial_reason("99887766@lid", _no_map) is not None

    def test_group_needs_exact_jid(self, monkeypatch):
        monkeypatch.setenv(outbound_policy.ALLOWED_RECIPIENTS_ENV, "120363000000000001@g.us")
        assert outbound_policy.denial_reason("120363000000000001@g.us", _no_map) is None
        assert outbound_policy.denial_reason("120363000000000002@g.us", _no_map) is not None
        # A group id must not match as if it were a phone number.
        assert outbound_policy.denial_reason("120363000000000001", _no_map) is not None

    def test_unparseable_allowlist_fails_closed(self, monkeypatch):
        monkeypatch.setenv(outbound_policy.ALLOWED_RECIPIENTS_ENV, "alice, bob")
        assert outbound_policy.denial_reason("12025551234", _no_map) is not None


class TestRemoveOutboundTools:
    def test_hides_only_send_tools(self):
        server = FastMCP("t")
        for name in (*outbound_policy.OUTBOUND_TOOLS, "list_messages", "download_media"):
            server.tool(name=name)(lambda: None)

        removed = outbound_policy.remove_outbound_tools(server)

        assert set(removed) == set(outbound_policy.OUTBOUND_TOOLS)
        remaining = {t.name for t in server._tool_manager.list_tools()}
        assert remaining == {"list_messages", "download_media"}

    def test_missing_tools_are_ignored(self):
        assert outbound_policy.remove_outbound_tools(FastMCP("t")) == []

    def test_every_listed_tool_exists_in_main(self):
        """Guards against a renamed/added send tool silently escaping read-only mode."""
        import main

        registered = {t.name for t in main.mcp._tool_manager.list_tools()}
        assert set(outbound_policy.OUTBOUND_TOOLS) <= registered


class _Response:
    status_code = 200
    text = "ok"

    def json(self):
        return {"success": True, "message": "sent", "ok": True}


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, url, json=None, headers=None):
        self.calls.append((url, json))
        return _Response()


@pytest.fixture
def post(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(whatsapp.requests, "post", rec)
    monkeypatch.setattr(whatsapp, "_sender_aliases", _no_map)
    return rec


@pytest.fixture
def media(tmp_path):
    f = tmp_path / "a.ogg"
    f.write_bytes(b"OggS")
    return str(f)


def _all_outbound(media):
    return {
        "send_message": lambda r: whatsapp.send_message(r, "hi"),
        "send_file": lambda r: whatsapp.send_file(r, media),
        "send_audio_message": lambda r: whatsapp.send_audio_message(r, media),
        "send_reaction": lambda r: whatsapp.send_reaction(r, "MSGID", "👍"),
        "mark_messages_read": lambda r: whatsapp.mark_messages_read(["MSGID"], r),
    }


class TestEnforcedInWhatsappLayer:
    def test_all_five_paths_blocked_in_read_only_and_never_reach_bridge(self, monkeypatch, post, media):
        monkeypatch.setenv(outbound_policy.READ_ONLY_ENV, "true")
        for name, call in _all_outbound(media).items():
            ok, message = call("12025551234@s.whatsapp.net")
            assert ok is False, name
            assert "Read-only" in message, name
        assert post.calls == []

    def test_all_five_paths_blocked_for_unlisted_recipient(self, monkeypatch, post, media):
        monkeypatch.setenv(outbound_policy.ALLOWED_RECIPIENTS_ENV, "12025551234")
        for name, call in _all_outbound(media).items():
            ok, message = call("12025559999@s.whatsapp.net")
            assert ok is False, name
            assert "WHATSAPP_ALLOWED_RECIPIENTS" in message, name
        assert post.calls == []

    def test_listed_recipient_still_sends(self, monkeypatch, post, media):
        monkeypatch.setenv(outbound_policy.ALLOWED_RECIPIENTS_ENV, "12025551234")
        for name, call in _all_outbound(media).items():
            ok, _ = call("12025551234@s.whatsapp.net")
            assert ok is True, name
        assert len(post.calls) == 5

    def test_audio_denied_before_ffmpeg_conversion(self, monkeypatch, post, tmp_path):
        """A non-.ogg file would be converted by ffmpeg first; denial must come before that work."""
        src = tmp_path / "voice.mp3"
        src.write_bytes(b"ID3")
        monkeypatch.setenv(outbound_policy.READ_ONLY_ENV, "true")
        monkeypatch.setattr(
            whatsapp.audio, "convert_to_opus_ogg_temp", lambda *a, **k: pytest.fail("ffmpeg ran while denied")
        )
        ok, _ = whatsapp.send_audio_message("12025551234", str(src))
        assert ok is False
