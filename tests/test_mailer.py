"""
Tests for the auth mail sender.

Two things are worth pinning down here. One is what counts as a complete SMTP
configuration, because the answer decides whether a mistake stops the app at
import or waits to raise inside somebody's password reset. The other is that a
send actually reaches the end of py4web's Mailer -- which sounds like testing
someone else's library, and is not: py4web 1.20260805.0 ships a Mailer whose
send() raises NameError on its first line, having dropped the pydal._compat
star-import that supplied add_charset, charset_QP, Header and Encoders. The app
imports and serves perfectly with that bug; only mail is dead.

So these tests strip those names back off the module and prove the repair
works, rather than asserting anything about whichever py4web happens to be
installed today.
"""

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
