import pytest
from download import parse_tweet_url


class TestParseTweetUrl:
    def test_standard_x_url(self):
        result = parse_tweet_url("https://x.com/elonmusk/status/1234567890")
        assert result == ("elonmusk", "1234567890")

    def test_twitter_url(self):
        result = parse_tweet_url("https://twitter.com/jack/status/9876543210")
        assert result == ("jack", "9876543210")

    def test_url_with_query_params(self):
        result = parse_tweet_url("https://x.com/user/status/111?s=20&t=abc")
        assert result == ("user", "111")

    def test_url_with_trailing_slash(self):
        result = parse_tweet_url("https://x.com/user/status/111/")
        assert result == ("user", "111")

    def test_mobile_url(self):
        result = parse_tweet_url("https://mobile.twitter.com/user/status/111")
        assert result == ("user", "111")

    def test_invalid_url_not_twitter(self):
        with pytest.raises(ValueError, match="Not a valid X/Twitter URL"):
            parse_tweet_url("https://youtube.com/watch?v=123")

    def test_invalid_url_no_status(self):
        with pytest.raises(ValueError, match="Not a valid X/Twitter URL"):
            parse_tweet_url("https://x.com/elonmusk")

    def test_invalid_url_empty(self):
        with pytest.raises(ValueError, match="Not a valid X/Twitter URL"):
            parse_tweet_url("")
