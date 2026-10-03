from app.tokens import extract_emails, generate_token, hash_token, is_valid_email, merge_emails


def test_extract_emails_from_messy_text():
    raw = "Max Mustermann <Max@Loge.de>; (anna@loge.de), bruder@loge.de\nmax@loge.de"
    assert extract_emails(raw) == ["anna@loge.de", "bruder@loge.de", "max@loge.de"]


def test_extract_emails_empty():
    assert extract_emails("") == []
    assert extract_emails(None) == []


def test_merge_is_additive_and_deduplicated():
    assert merge_emails(["a@x.de"], "b@x.de a@x.de") == ["a@x.de", "b@x.de"]


def test_is_valid_email():
    assert is_valid_email("a@b.de")
    assert not is_valid_email("a@b")


def test_tokens_are_random_and_only_hash_matches():
    raw1, hash1 = generate_token()
    raw2, _ = generate_token()
    assert raw1 != raw2
    assert hash_token(raw1) == hash1
    assert raw1 not in hash1 and len(raw1) >= 40
