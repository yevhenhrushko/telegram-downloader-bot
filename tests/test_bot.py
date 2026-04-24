import asyncio
import time

import bot


class FakeChat:
    def __init__(self):
        self.actions = []
        self.documents = []
        self.messages = []

    async def send_action(self, action):
        self.actions.append(action)

    async def send_document(self, document, filename):
        self.documents.append(filename)

    async def send_media_group(self, media):
        raise AssertionError("send_media_group should not be used in this test")

    async def send_message(self, text, **kwargs):
        self.messages.append((text, kwargs))


class FakeStatusMessage:
    def __init__(self, chat):
        self.chat = chat
        self.edits = []

    async def edit_text(self, text):
        self.edits.append(text)


def test_send_files_uses_plain_text_link_for_large_files(monkeypatch, tmp_path):
    file_path = tmp_path / "clip (1).mp4"
    file_path.write_bytes(b"ab")

    chat = FakeChat()
    status_msg = FakeStatusMessage(chat)

    monkeypatch.setattr(bot, "TELEGRAM_UPLOAD_LIMIT", 1)
    monkeypatch.setattr(bot, "_serve_large_file", lambda _: "http://example.com/files/clip%20%281%29.mp4")

    asyncio.run(bot._send_files(status_msg, [str(file_path)]))

    assert not chat.documents
    assert len(chat.messages) == 1
    text, kwargs = chat.messages[0]
    assert "clip (1).mp4" in text
    assert "http://example.com/files/clip%20%281%29.mp4" in text
    assert "parse_mode" not in kwargs
    assert kwargs["disable_web_page_preview"] is True
    assert status_msg.edits == ["Done: 0 sent, 1 as download link(s)"]


def test_send_files_continues_when_send_action_fails(monkeypatch, tmp_path):
    file_path = tmp_path / "small.mp4"
    file_path.write_bytes(b"abc")

    chat = FakeChat()
    status_msg = FakeStatusMessage(chat)

    async def failing_send_action(action):
        raise RuntimeError("action failed")

    chat.send_action = failing_send_action
    monkeypatch.setattr(bot, "TELEGRAM_UPLOAD_LIMIT", 10)

    asyncio.run(bot._send_files(status_msg, [str(file_path)]))

    assert chat.documents == ["small.mp4"]
    assert status_msg.edits == ["Done: 1 sent"]


def test_serve_large_file_url_encodes_filename(monkeypatch, tmp_path):
    nginx_dir = tmp_path / "nginx"
    source = tmp_path / "clip (1).mp4"
    source.write_bytes(b"file")

    monkeypatch.setattr(bot, "NGINX_DIR", nginx_dir)
    monkeypatch.setattr(bot, "SERVER_URL", "http://example.com/files/")

    link = bot._serve_large_file(str(source))

    assert link == "http://example.com/files/clip%20%281%29.mp4"
    assert not source.exists()
    assert (nginx_dir / "clip (1).mp4").exists()


def test_run_download_shows_heartbeat_without_progress(monkeypatch):
    chat = FakeChat()
    status_msg = FakeStatusMessage(chat)

    def fake_download_media(url, force, mp3, progress_callback):
        time.sleep(0.05)
        return []

    async def fake_send_files(status_msg, saved):
        return None

    monkeypatch.setattr(bot, "download_media", fake_download_media)
    monkeypatch.setattr(bot, "_send_files", fake_send_files)
    monkeypatch.setattr(bot, "PROGRESS_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(bot, "PROGRESS_HEARTBEAT_SECONDS", 0.02)

    asyncio.run(bot._run_download(None, status_msg, "https://www.instagram.com/p/ABC123/"))

    assert any(edit.startswith("Still downloading from instagram...") for edit in status_msg.edits)
