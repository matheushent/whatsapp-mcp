"""Tests for rendering media as image content.

The gap these cover: download_media hands back a file path, which a client
without filesystem access cannot open, so media was unreachable for it. The
rendering path has to stay honest about what it cannot do — silently returning
nothing would look identical to "there is no media".
"""

import subprocess

import pytest

import media_preview


def _write(path, data=b"x"):
    path.write_bytes(data)
    return path


def test_audio_is_rejected_with_a_pointer_to_the_transcript(tmp_path):
    voice_note = _write(tmp_path / "audio_1.ogg")
    with pytest.raises(media_preview.PreviewError) as excinfo:
        media_preview.render_preview(voice_note)
    assert "transcribe_audio" in str(excinfo.value)


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(media_preview.PreviewError) as excinfo:
        media_preview.render_preview(tmp_path / "gone.jpg")
    assert "not found" in str(excinfo.value).lower()


def test_small_image_passes_through_without_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(media_preview.shutil, "which", lambda _: None)
    image = _write(tmp_path / "image_1.png", b"\x89PNG\r\n\x1a\npng-bytes")
    data, image_format = media_preview.render_preview(image)
    assert data == b"\x89PNG\r\n\x1a\npng-bytes"
    assert image_format == "png"


def test_jpg_suffix_is_normalised_to_jpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(media_preview.shutil, "which", lambda _: None)
    image = _write(tmp_path / "image_1.jpg", b"\xff\xd8\xffjpeg-bytes")
    _, image_format = media_preview.render_preview(image)
    assert image_format == "jpeg"


@pytest.mark.parametrize(
    ("data", "expected_format"),
    [
        (b"\xff\xd8\xffjpeg", "jpeg"),
        (b"\x89PNG\r\n\x1a\npng", "png"),
        (b"GIF89agif", "gif"),
        (b"RIFF\x00\x00\x00\x00WEBPwebp", "webp"),
    ],
)
def test_image_format_from_bytes_recognises_supported_formats(data, expected_format):
    assert media_preview.image_format_from_bytes(data) == expected_format


def test_unknown_jpg_bytes_without_ffmpeg_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(media_preview.shutil, "which", lambda _: None)
    image = _write(tmp_path / "generated.jpg", b"not an image")
    with pytest.raises(media_preview.PreviewError, match="not a supported image"):
        media_preview.render_preview(image)


def test_file_that_grows_after_size_check_is_not_returned_without_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(media_preview.shutil, "which", lambda _: None)
    image = _write(tmp_path / "generated.jpg", b"\x89PNG\r\n\x1a\nsmall")
    original_stat = media_preview.Path.stat
    size_checks = 0

    def grow_before_read(path, *args, **kwargs):
        nonlocal size_checks
        result = original_stat(path, *args, **kwargs)
        if path == image:
            size_checks += 1
            if size_checks == 2:
                with open(image, "ab") as media_file:
                    media_file.write(b"0" * media_preview.MAX_PASSTHROUGH_BYTES)
        return result

    monkeypatch.setattr(media_preview.Path, "stat", grow_before_read)
    with pytest.raises(media_preview.PreviewError, match="size worth returning"):
        media_preview.render_preview(image)


def test_large_file_without_ffmpeg_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(media_preview.shutil, "which", lambda _: None)
    image = _write(tmp_path / "image_1.jpg", b"0" * (media_preview.MAX_PASSTHROUGH_BYTES + 1))
    with pytest.raises(media_preview.PreviewError) as excinfo:
        media_preview.render_preview(image)
    assert "ffmpeg" in str(excinfo.value)


def test_video_without_ffmpeg_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(media_preview.shutil, "which", lambda _: None)
    video = _write(tmp_path / "video_1.mp4")
    with pytest.raises(media_preview.PreviewError):
        media_preview.render_preview(video)


def test_scale_filter_never_enlarges():
    assert "force_original_aspect_ratio=decrease" in media_preview.scale_filter(800)
    assert "min(800,iw)" in media_preview.scale_filter(800)


def test_command_takes_a_single_frame_so_video_and_image_share_one_path(tmp_path):
    command = media_preview.ffmpeg_command(tmp_path / "in.mp4", tmp_path / "out.jpg", 640)
    assert command[:2] == ["ffmpeg", "-v"]
    assert "-frames:v" in command and command[command.index("-frames:v") + 1] == "1"
    assert media_preview.scale_filter(640) in command


def test_rendered_bytes_are_returned_and_the_temp_file_is_cleaned_up(tmp_path, monkeypatch):
    monkeypatch.setattr(media_preview.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    source = _write(tmp_path / "video_1.mp4")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    def fake_run(command, **kwargs):
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["timeout"] == media_preview.FFMPEG_TIMEOUT_SECONDS
        assert kwargs["shell"] is False
        destination = media_preview.Path(command[-1])
        destination.write_bytes(b"rendered-jpeg")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(media_preview.subprocess, "run", fake_run)
    data, image_format = media_preview.render_preview(source, work_dir=work_dir)
    assert (data, image_format) == (b"rendered-jpeg", "jpeg")
    assert list(work_dir.iterdir()) == []


def test_render_timeout_is_reported_and_the_temp_file_is_cleaned_up(tmp_path, monkeypatch):
    monkeypatch.setattr(media_preview.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    source = _write(tmp_path / "video_1.mp4")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    def fake_run(command, **_kwargs):
        media_preview.Path(command[-1]).write_bytes(b"partial-preview")
        raise subprocess.TimeoutExpired(command, media_preview.FFMPEG_TIMEOUT_SECONDS)

    monkeypatch.setattr(media_preview.subprocess, "run", fake_run)
    with pytest.raises(media_preview.PreviewError, match="within 60 seconds"):
        media_preview.render_preview(source, work_dir=work_dir)
    assert list(work_dir.iterdir()) == []


@pytest.mark.parametrize("max_dimension", [0, -1, 2049, 1.5, True])
def test_invalid_max_dimension_is_rejected(max_dimension):
    with pytest.raises(media_preview.PreviewError, match="1 to 2048"):
        media_preview.validate_max_dimension(max_dimension)


def test_ffmpeg_failure_surfaces_its_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(media_preview.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    source = _write(tmp_path / "document_1.pdf")

    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1, b"", b"Invalid data found")

    monkeypatch.setattr(media_preview.subprocess, "run", fake_run)
    with pytest.raises(media_preview.PreviewError) as excinfo:
        media_preview.render_preview(source)
    assert "Invalid data found" in str(excinfo.value)


def _flag_value(command, flag):
    return command[command.index(flag) + 1]


def test_preview_command_pins_input_to_local_files_and_bounds_resources(tmp_path):
    """Sender-controlled bytes reach ffmpeg: only the file protocol, capped allocations and output."""
    command = media_preview.ffmpeg_command(tmp_path / "in.mp4", tmp_path / "out.jpg", 640)
    assert _flag_value(command, "-protocol_whitelist") == "file"
    assert int(_flag_value(command, "-max_alloc")) > 0
    assert int(_flag_value(command, "-fs")) == media_preview.FFMPEG_MAX_OUTPUT_BYTES
    assert "-nostdin" in command
    # Guards must be input options: they only take effect when placed before -i.
    assert command.index("-protocol_whitelist") < command.index("-i")
    assert command.index("-max_alloc") < command.index("-i")
    assert command[-1] == str(tmp_path / "out.jpg")


@pytest.mark.skipif(not media_preview.ffmpeg_available(), reason="ffmpeg not installed")
def test_guarded_command_still_renders_a_real_image(tmp_path):
    """The hardening flags must be accepted by a real ffmpeg, not just present in the argv."""
    source = tmp_path / "photo.jpg"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", str(source)],
        check=True,
    )
    data, image_format = media_preview.render_preview(source, max_dimension=32, work_dir=tmp_path)
    assert image_format == "jpeg"
    assert data.startswith(b"\xff\xd8\xff")


@pytest.mark.skipif(not media_preview.ffmpeg_available(), reason="ffmpeg not installed")
def test_playlist_pointing_at_a_network_url_never_makes_a_request(tmp_path):
    """A sender can name an HLS playlist 'photo.jpg'. It must not make ffmpeg reach out over the network."""
    import http.server
    import threading

    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        source = tmp_path / "evil.m3u8"
        source.write_text(
            "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n"
            f"http://127.0.0.1:{server.server_address[1]}/ssrf.ts\n#EXT-X-ENDLIST\n"
        )
        with pytest.raises(media_preview.PreviewError):
            media_preview.render_preview(source, work_dir=tmp_path)
    finally:
        server.shutdown()
    assert hits == []
