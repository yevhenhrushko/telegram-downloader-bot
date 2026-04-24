import subprocess
import time

import pytest
import download
from download import (
    DownloadError,
    _check_disk_space,
    _download_instagram,
    _format_duration,
    _summarize_cookie_health,
    build_filenames,
    detect_platform,
    download_media,
    ensure_instagram_cookies_valid,
    parse_instagram_url,
    parse_telegram_url,
    parse_tweet_url,
    parse_youtube_url,
)


class TestDetectPlatform:
    def test_x_com(self):
        assert detect_platform("https://x.com/user/status/123") == "twitter"

    def test_twitter_com(self):
        assert detect_platform("https://twitter.com/user/status/123") == "twitter"

    def test_mobile_twitter(self):
        assert detect_platform("https://mobile.twitter.com/user/status/123") == "twitter"

    def test_instagram(self):
        assert detect_platform("https://www.instagram.com/p/ABC123/") == "instagram"

    def test_instagram_no_www(self):
        assert detect_platform("https://instagram.com/reel/ABC123/") == "instagram"

    def test_telegram(self):
        assert detect_platform("https://t.me/channel/123") == "telegram"

    def test_web_telegram(self):
        assert detect_platform("https://web.telegram.org/a/#-1002899724101") == "telegram"

    def test_youtube(self):
        assert detect_platform("https://www.youtube.com/watch?v=abc123") == "youtube"

    def test_youtu_be(self):
        assert detect_platform("https://youtu.be/abc123") == "youtube"

    def test_youtube_mobile(self):
        assert detect_platform("https://m.youtube.com/watch?v=abc123") == "youtube"

    def test_youtube_music(self):
        assert detect_platform("https://music.youtube.com/watch?v=abc123") == "youtube"

    def test_unsupported(self):
        with pytest.raises(ValueError, match="Unsupported platform"):
            detect_platform("https://vimeo.com/123456")

    def test_empty(self):
        with pytest.raises(ValueError):
            detect_platform("")


class TestParseTweetUrl:
    def test_standard_x_url(self):
        assert parse_tweet_url("https://x.com/elonmusk/status/1234567890") == ("elonmusk", "1234567890")

    def test_twitter_url(self):
        assert parse_tweet_url("https://twitter.com/jack/status/9876543210") == ("jack", "9876543210")

    def test_url_with_query_params(self):
        assert parse_tweet_url("https://x.com/user/status/111?s=20&t=abc") == ("user", "111")

    def test_url_with_trailing_slash(self):
        assert parse_tweet_url("https://x.com/user/status/111/") == ("user", "111")

    def test_mobile_url(self):
        assert parse_tweet_url("https://mobile.twitter.com/user/status/111") == ("user", "111")

    def test_invalid_url_not_twitter(self):
        with pytest.raises(ValueError, match="Not a valid X/Twitter URL"):
            parse_tweet_url("https://youtube.com/watch?v=123")

    def test_invalid_url_no_status(self):
        with pytest.raises(ValueError, match="Not a valid X/Twitter URL"):
            parse_tweet_url("https://x.com/elonmusk")

    def test_invalid_url_empty(self):
        with pytest.raises(ValueError, match="Not a valid X/Twitter URL"):
            parse_tweet_url("")


class TestParseInstagramUrl:
    def test_post(self):
        assert parse_instagram_url("https://www.instagram.com/p/ABC123/") == (None, "ABC123")

    def test_reel(self):
        assert parse_instagram_url("https://www.instagram.com/reel/XYZ789/") == (None, "XYZ789")

    def test_reels(self):
        assert parse_instagram_url("https://instagram.com/reels/DEF456/") == (None, "DEF456")

    def test_story(self):
        assert parse_instagram_url("https://www.instagram.com/stories/natgeo/1234567890") == ("natgeo", "1234567890")

    def test_post_no_trailing_slash(self):
        assert parse_instagram_url("https://instagram.com/p/ABC123") == (None, "ABC123")

    def test_invalid(self):
        with pytest.raises(ValueError, match="Not a valid Instagram URL"):
            parse_instagram_url("https://instagram.com/user")

    def test_empty(self):
        with pytest.raises(ValueError, match="Not a valid Instagram URL"):
            parse_instagram_url("")


class TestParseTelegramUrl:
    # Single message URLs
    def test_public_channel_message(self):
        assert parse_telegram_url("https://t.me/durov/123") == ("durov", "123")

    def test_private_channel_message(self):
        assert parse_telegram_url("https://t.me/c/1234567890/456") == ("c/1234567890", "456")

    def test_trailing_slash(self):
        assert parse_telegram_url("https://t.me/channel/789/") == ("channel", "789")

    # Full channel URLs (message_id=None)
    def test_public_channel_only(self):
        assert parse_telegram_url("https://t.me/durov") == ("durov", None)

    def test_private_channel_only(self):
        assert parse_telegram_url("https://t.me/c/1234567890") == ("c/1234567890", None)

    # web.telegram.org URLs
    def test_web_telegram_channel(self):
        assert parse_telegram_url("https://web.telegram.org/a/#-1002899724101") == ("c/2899724101", None)

    def test_web_telegram_message(self):
        assert parse_telegram_url("https://web.telegram.org/a/#-1002899724101/739") == ("c/2899724101", "739")

    def test_invalid_empty(self):
        with pytest.raises(ValueError, match="Not a valid Telegram URL"):
            parse_telegram_url("")


class TestBuildFilenames:
    def test_single_video(self):
        assert build_filenames("elonmusk", "123", ["video.mp4"]) == {"video.mp4": "@elonmusk_123.mp4"}

    def test_single_image(self):
        assert build_filenames("user", "456", ["photo.jpg"]) == {"photo.jpg": "@user_456.jpg"}

    def test_multiple_media(self):
        assert build_filenames("user", "789", ["a.jpg", "b.png", "c.mp4"]) == {
            "a.jpg": "@user_789_1.jpg",
            "b.png": "@user_789_2.png",
            "c.mp4": "@user_789_3.mp4",
        }

    def test_preserves_extension(self):
        assert build_filenames("user", "1", ["file.webm"]) == {"file.webm": "@user_1.webm"}


class TestParseYoutubeUrl:
    def test_standard_watch(self):
        assert parse_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == ("dQw4w9WgXcQ", None)

    def test_short_url(self):
        assert parse_youtube_url("https://youtu.be/dQw4w9WgXcQ") == ("dQw4w9WgXcQ", None)

    def test_shorts(self):
        vid, pl = parse_youtube_url("https://www.youtube.com/shorts/dQw4w9WgXcQ")
        assert vid == "dQw4w9WgXcQ"
        assert pl is None

    def test_playlist_only(self):
        vid, pl = parse_youtube_url("https://www.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf")
        assert vid == ""
        assert pl == "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"

    def test_video_in_playlist(self):
        vid, pl = parse_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf")
        assert vid == "dQw4w9WgXcQ"
        assert pl == "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"

    def test_live(self):
        vid, pl = parse_youtube_url("https://www.youtube.com/live/dQw4w9WgXcQ")
        assert vid == "dQw4w9WgXcQ"

    def test_mobile(self):
        assert parse_youtube_url("https://m.youtube.com/watch?v=dQw4w9WgXcQ") == ("dQw4w9WgXcQ", None)

    def test_youtu_be_with_playlist(self):
        vid, pl = parse_youtube_url("https://youtu.be/dQw4w9WgXcQ?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf")
        assert vid == "dQw4w9WgXcQ"
        assert pl == "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"

    def test_invalid(self):
        with pytest.raises(ValueError, match="Not a valid YouTube URL"):
            parse_youtube_url("https://www.youtube.com/channel/UCxxxx")

    def test_invalid_empty(self):
        with pytest.raises(ValueError, match="Not a valid YouTube URL"):
            parse_youtube_url("")


class TestFormatDuration:
    def test_zero(self):
        assert _format_duration(0) == "unknown"

    def test_none(self):
        assert _format_duration(None) == "unknown"

    def test_seconds_only(self):
        assert _format_duration(30) == "0:30"

    def test_minutes_and_seconds(self):
        assert _format_duration(65) == "1:05"

    def test_hours(self):
        assert _format_duration(3661) == "1:01:01"

    def test_exact_minute(self):
        assert _format_duration(60) == "1:00"


class TestDiskSpaceCheck:
    def test_enough_space(self):
        # Should not raise — the dev machine always has >500MB
        _check_disk_space(min_mb=1)

    def test_not_enough_space(self):
        with pytest.raises(DownloadError, match="Not enough disk space"):
            _check_disk_space(min_mb=999_999_999)


class TestDownloadMediaErrors:
    def test_invalid_instagram_api_url_is_download_error(self):
        url = "https://www.instagram.com/api/v1/media/3878201540318090661/info/"
        with pytest.raises(DownloadError, match="Not a valid Instagram URL"):
            download_media(url, force=True)

    def test_instagram_timeout_becomes_download_error(self, monkeypatch, tmp_path):
        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

        monkeypatch.setattr(download.subprocess, "run", fake_run)
        monkeypatch.setattr(download, "GALLERY_DL_TIMEOUT_SECONDS", 12)

        with pytest.raises(DownloadError, match="Instagram download timed out after 12s"):
            _download_instagram("https://www.instagram.com/p/ABC123/", str(tmp_path))

    def test_instagram_emits_initial_progress_message(self, monkeypatch, tmp_path):
        seen = []

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

        monkeypatch.setattr(download.subprocess, "run", fake_run)
        missing_path = tmp_path / "missing-instagram-cookies.txt"
        monkeypatch.setitem(download.COOKIES_FILES, "instagram", missing_path)

        _download_instagram(
            "https://www.instagram.com/p/ABC123/",
            str(tmp_path),
            progress_callback=lambda phase, value: seen.append((phase, value)),
        )

        assert ("info", "Checking Instagram cookies...") in seen
        assert ("info", "Instagram cookies missing. Trying public access...") in seen


class TestInstagramCookieHealth:
    def test_instagram_cookie_summary_flags_missing_sessionid(self, monkeypatch, tmp_path):
        cookie_path = tmp_path / "instagram-cookies.txt"
        cookie_path.write_text(
            "# Netscape HTTP Cookie File\n"
            ".instagram.com\tTRUE\t/\tFALSE\t4102444800\tcsrftoken\tfake\n"
        )
        monkeypatch.setitem(download.COOKIES_FILES, "instagram", cookie_path)

        status, summary = _summarize_cookie_health("instagram")

        assert status == "invalid"
        assert "sessionid" in summary
        with pytest.raises(DownloadError, match="missing a sessionid cookie"):
            ensure_instagram_cookies_valid()

    def test_instagram_cookie_summary_flags_expired_session(self, monkeypatch, tmp_path):
        cookie_path = tmp_path / "instagram-cookies.txt"
        expired = int(time.time()) - 60
        cookie_path.write_text(
            "# Netscape HTTP Cookie File\n"
            f".instagram.com\tTRUE\t/\tFALSE\t{expired}\tsessionid\tfake\n"
        )
        monkeypatch.setitem(download.COOKIES_FILES, "instagram", cookie_path)

        status, summary = _summarize_cookie_health("instagram")

        assert status == "expired"
        assert "sessionid" in summary
        with pytest.raises(DownloadError, match="Instagram cookies are expired"):
            ensure_instagram_cookies_valid()
