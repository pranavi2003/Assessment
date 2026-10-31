from app.gateway import StreamingRedactor

def test_email_split_across_chunks():
    r = StreamingRedactor()
    out = r.feed("Reach alex.morgan@")
    out += r.feed("example.com today.")
    out += r.feed("", final=True)
    assert "alex.morgan@example.com" not in out
    assert "[REDACTED]" in out

def test_ssn_redacted():
    r = StreamingRedactor()
    out = r.feed("SSN 123-45-6789")
    out += r.feed("", final=True)
    assert "123-45-6789" not in out
    assert "[REDACTED]" in out
