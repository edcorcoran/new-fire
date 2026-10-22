"""
Tests for the auth mail sender.

Three things are worth pinning down here. One is what counts as a complete SMTP
configuration, because the answer decides whether a mistake stops the app at
import or waits to raise inside somebody's password reset. Another is that a
send actually reaches the end of py4web's Mailer -- which sounds like testing
someone else's library, and is not: py4web 1.20260805.0 ships a Mailer whose
send() raises NameError on its first line, having dropped the pydal._compat
star-import that supplied add_charset, charset_QP, Header and Encoders. The app
imports and serves perfectly with that bug; only mail is dead.

So these tests strip those names back off the module and prove the repair
works, rather than asserting anything about whichever py4web happens to be
installed today.

The third is that the SMTP connection verifies the server's certificate. That
one has no visible symptom at all -- mail sends perfectly either way -- and the
cost of getting it wrong is the mailbox password, so it needs the test more
than anything else in this file does.
"""

import smtplib
import ssl

import mailer
import pytest
from py4web.utils import mailer as py4web_mailer


class Settings:
    """The five names build_mailer reads, with email switched off."""

    SMTP_SERVER = None
    SMTP_SENDER = None
    SMTP_LOGIN = None
    SMTP_SSL = True
    SMTP_TLS = False
    SMTP_TIMEOUT = 30


def settings(**overrides):
    return type("Overridden", (Settings,), overrides)


def test_no_server_means_no_sender():
    # Not an error: auth then prints the message it would have sent, which is
    # what an unconfigured checkout should do.
    assert mailer.build_mailer(settings()) is None


def test_sender_is_required_alongside_a_server():
    with pytest.raises(RuntimeError, match="SMTP_SENDER"):
        mailer.build_mailer(settings(SMTP_SERVER="smtp.example.com:465"))


def test_login_is_required_for_a_real_server():
    with pytest.raises(RuntimeError, match="SMTP_LOGIN"):
        mailer.build_mailer(
            settings(SMTP_SERVER="smtp.example.com:465", SMTP_SENDER="a@b.c")
        )


def test_login_must_carry_a_password():
    # "username:password", so a login without the colon is a truncated one --
    # the failure it would otherwise cause happens at the far end of a socket.
    with pytest.raises(RuntimeError, match="colon"):
        mailer.build_mailer(
            settings(
                SMTP_SERVER="smtp.example.com:465",
                SMTP_SENDER="a@b.c",
                SMTP_LOGIN="a@b.c",
            )
        )


def test_password_may_contain_colons():
    # Mailer splits on the first colon only, so this is a real password rather
    # than a malformed setting, and validation must not reject it.
    built = mailer.build_mailer(
        settings(
            SMTP_SERVER="smtp.example.com:465",
            SMTP_SENDER="a@b.c",
            SMTP_LOGIN="a@b.c:pw:with:colons",
        )
    )
    assert built.settings.login.split(":", 1)[1] == "pw:with:colons"


def test_logging_server_needs_no_credentials():
    # What development runs: the message goes to the log instead of a socket,
    # so no mailbox password has to exist on the machine.
    assert mailer.build_mailer(settings(SMTP_SERVER="logging", SMTP_SENDER="a@b.c"))


def test_timeout_overrides_the_five_seconds_py4web_hardcodes():
    built = mailer.build_mailer(settings(SMTP_SERVER="logging", SMTP_SENDER="a@b.c"))
    assert built.settings.timeout == Settings.SMTP_TIMEOUT


def test_send_works_when_py4web_is_missing_its_compat_names(monkeypatch):
    """The regression test for py4web 1.20260805.0.

    Strips the four names off py4web's mailer module -- reproducing that
    release on any version -- and then sends. Without restore_compat_names this
    raises NameError: add_charset, from the first line of send(), which is what
    a password reset on an unrepaired deployment does.
    """
    for name in mailer.COMPAT_NAMES:
        monkeypatch.delattr(py4web_mailer, name, raising=False)

    assert sorted(mailer.restore_compat_names()) == sorted(mailer.COMPAT_NAMES)

    built = mailer.build_mailer(settings(SMTP_SERVER="logging", SMTP_SENDER="a@b.c"))
    assert built.send(to="x@y.z", subject="test", body="body") is True


def test_restoring_is_a_no_op_when_py4web_is_whole():
    # The guard that lets this ship to machines running either release, and the
    # signal for when the workaround can be deleted.
    mailer.restore_compat_names()
    assert mailer.restore_compat_names() == []


# ------------------------------------------------------------- TLS verification
#
# local_hostname is pinned throughout: smtplib otherwise calls socket.getfqdn()
# in its constructor, which is a reverse DNS lookup the test has no reason to
# wait for. None of these open a socket -- smtplib only dials when it is given
# a host -- so the certificate logic can be inspected without a server.


def test_py4web_builds_its_smtp_client_from_the_verifying_classes():
    """The integration point, and the only one there is.

    Mailer.send() reads these two names off its module's `smtplib` when it is
    about to connect, so this is what decides whether a real send verifies.
    """
    assert py4web_mailer.smtplib.SMTP_SSL is mailer.VerifyingSMTP_SSL
    assert py4web_mailer.smtplib.SMTP is mailer.VerifyingSMTP


def test_implicit_ssl_verifies_the_certificate():
    # The 465 path, which is what this deployment uses.
    client = mailer.VerifyingSMTP_SSL(local_hostname="localhost")
    assert client.context.verify_mode == ssl.CERT_REQUIRED
    assert client.context.check_hostname is True


def test_starttls_verifies_the_certificate(monkeypatch):
    # The 587 path. starttls() is where the handshake happens, so the context
    # has to be caught on its way into the stdlib rather than read off the
    # object afterwards.
    captured = {}

    def fake_starttls(self, *, context=None):
        captured["context"] = context
        return (220, b"ready to start TLS")

    monkeypatch.setattr(smtplib.SMTP, "starttls", fake_starttls)

    mailer.VerifyingSMTP(local_hostname="localhost").starttls()

    assert captured["context"].verify_mode == ssl.CERT_REQUIRED
    assert captured["context"].check_hostname is True


def test_an_explicit_context_is_not_overridden():
    # So that the day py4web grows a setting for this, passing one through
    # still means what it says -- including deliberately passing a lax one.
    mine = ssl._create_unverified_context()
    client = mailer.VerifyingSMTP_SSL(local_hostname="localhost", context=mine)
    assert client.context is mine


def test_the_stand_in_is_otherwise_the_real_smtplib():
    # Mailer only looks up the two classes today, but it is handed a whole
    # module; everything it did not come for has to still be there.
    assert py4web_mailer.smtplib.SMTPAuthenticationError is (
        smtplib.SMTPAuthenticationError
    )
    assert py4web_mailer.smtplib.SMTP_SSL_PORT == smtplib.SMTP_SSL_PORT


def test_the_stdlib_default_this_replaces_verifies_nothing():
    """Why the three tests above exist, and the signal for when they can go.

    smtplib handed no context builds ssl._create_stdlib_context(), which
    verifies neither the chain nor the hostname. If CPython ever changes that,
    this is the test that fails, and install_certificate_verification can be
    deleted rather than quietly kept forever.
    """
    plain = smtplib.SMTP_SSL(local_hostname="localhost")
    assert plain.context.verify_mode == ssl.CERT_NONE
    assert plain.context.check_hostname is False
