"""Offline tests: no real mailbox, SMTP server or Support Desk. Run: python -m unittest -v"""
import base64
import os
import sys
import unittest
from email.message import EmailMessage
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import support_mail as sm  # noqa: E402

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==") * 400


def mail(frm="Sunny Patel <sunny@greatharvest.com>", subject="Close will not save", body="It says Not saved.", headers=None, image=False, html=None):
    m = EmailMessage()
    m["From"] = frm
    m["To"] = "support@p2unlimited.com"
    m["Subject"] = subject
    m["Message-ID"] = "<abc@mail>"
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    if image:
        m.add_attachment(PNG, maintype="image", subtype="png", filename="shot.png")
    return m.as_bytes()


class Parse(unittest.TestCase):
    def test_basic(self):
        p = sm.parse_message(mail(image=True))
        self.assertEqual(p["from"], "sunny@greatharvest.com")
        self.assertEqual(p["fromName"], "Sunny Patel")
        self.assertEqual(p["messageId"], "<abc@mail>")
        self.assertIn("Not saved", p["text"])
        self.assertEqual(len(p["images"]), 1)
        self.assertEqual(p["images"][0]["type"], "image/png")

    def test_html_only(self):
        m = EmailMessage()
        m["From"] = "a@b.com"
        m["Subject"] = "x"
        m.set_content("<html><head><style>p{}</style></head><body><p>Hello</p><p>The <b>Plan</b> is blank</p></body></html>", subtype="html")
        p = sm.parse_message(m.as_bytes())
        self.assertIn("The Plan is blank", p["text"])
        self.assertNotIn("p{}", p["text"])

    def test_reply_to_wins(self):
        p = sm.parse_message(mail(headers={"Reply-To": "Store <store@greatharvest.com>"}))
        self.assertEqual(p["from"], "store@greatharvest.com")


class Skip(unittest.TestCase):
    def reason(self, **kw):
        return sm.skip_reason(sm.parse_message(mail(**kw)), "support@p2unlimited.com")

    def test_normal(self):
        self.assertEqual(self.reason(), "")

    def test_guards(self):
        self.assertEqual(self.reason(headers={"Auto-Submitted": "auto-replied"}), "auto-submitted")
        self.assertEqual(self.reason(headers={"Auto-Submitted": "no"}), "")
        self.assertEqual(self.reason(subject="Out of Office: back Monday"), "automatic subject")
        self.assertEqual(self.reason(subject="Automatic reply: Close"), "automatic subject")
        self.assertEqual(self.reason(frm="MAILER-DAEMON@x.com"), "no-reply sender")
        self.assertEqual(self.reason(frm="no-reply@x.com"), "no-reply sender")
        self.assertEqual(self.reason(frm="support@p2unlimited.com"), "from ourselves")
        self.assertEqual(self.reason(headers={"Precedence": "bulk"}), "bulk precedence")
        self.assertEqual(self.reason(headers={"List-Unsubscribe": "<mailto:x>"}), "mailing list")
        self.assertEqual(self.reason(headers={"X-Autoreply": "yes"}), "autoresponder header")


class Build(unittest.TestCase):
    def test_ack_headers(self):
        m = sm.build_message("P2UN Support", "support@p2unlimited.com", "sunny@x.com", "Re: Hi [GH-0001]", "Body", "<abc@mail>", auto=True)
        self.assertEqual(m["Auto-Submitted"], "auto-replied")
        self.assertEqual(m["In-Reply-To"], "<abc@mail>")
        self.assertTrue(m["Message-ID"].endswith("@p2unlimited.com>"))


class FakeIMAP:
    def __init__(self, messages, keywords=True):
        self.messages = messages  # uid -> raw
        self.flags = {u: set() for u in messages}
        self.appended = []
        self.untagged_responses = {"PERMANENTFLAGS": [b"(\\Seen \\Draft \\*)" if keywords else b"(\\Seen \\Draft)"]}
        self.searches = []

    def login(self, u, p): return "OK", [b""]
    def select(self, f): return "OK", [b"3"]
    def list(self): return "OK", [b'(\\HasNoChildren) "." INBOX', b'(\\HasNoChildren \\Drafts) "." "INBOX.Drafts"', b'(\\HasNoChildren \\Sent) "." "INBOX.Sent"']
    def logout(self): pass

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            self.searches.append(args[1])
            uids = [u for u in self.messages if not ({"P2UNLogged", "P2UNSkipped"} & self.flags[u])] if "UNKEYWORD" in args[1] else list(self.messages)
            return "OK", [b" ".join(uids)]
        if cmd == "FETCH":
            u = args[0]
            return "OK", [(("%s (UID %s FLAGS (%s) BODY[] {1}" % (u.decode(), u.decode(), " ".join(self.flags[u]))).encode(), self.messages[u]), b")"]
        if cmd == "STORE":
            self.flags[args[0]].add(args[2].strip("()"))
            return "OK", [b""]

    def append(self, folder, flags, date, raw):
        self.appended.append((folder, flags, raw))
        return "OK", [b""]


class FakeDesk:
    def __init__(self):
        self.calls = []
        self.outbox = [{"id": "O-0002", "ticketId": "GH-0001", "kind": "Reply", "mode": "draft", "to": "maria@x.com", "subject": "Your report GH-0001 [GH-0001]", "body": "Hi", "inReplyTo": ""},
                       {"id": "O-0003", "ticketId": "GH-0002", "kind": "Ack", "mode": "send", "to": "bob@x.com", "subject": "Your report GH-0002 [GH-0002]", "body": "Thanks", "inReplyTo": ""}]
        self.n = 0

    def call(self, action, **p):
        self.calls.append((action, p))
        if action == "email_in":
            self.n += 1
            return {"ok": True, "ticketId": "GH-%04d" % (10 + self.n), "existing": False, "ack": True, "ackTo": p["from"], "ackSubject": "Re: x [GH-0011]", "ackBody": "Thanks", "inReplyTo": p["messageId"]}
        if action == "outbox":
            return {"ok": True, "items": self.outbox}
        return {"ok": True}


class Run(unittest.TestCase):
    def setUp(self):
        os.environ.update({"MAIL_USER": "support@p2unlimited.com", "MAIL_PASSWORD": "x", "SUPPORT_ENDPOINT": "https://x", "SUPPORT_TOKEN": "t"})

    def go(self, imap, desk, dry=False):
        sent = []
        with mock.patch.object(sm.imaplib, "IMAP4_SSL", return_value=imap), \
             mock.patch.object(sm, "Desk", return_value=desk), \
             mock.patch.object(sm, "smtp_send", side_effect=lambda m: sent.append(m)):
            stats = sm.run(dry=dry)
        return stats, sent

    def test_full_pass(self):
        imap = FakeIMAP({b"1": mail(image=True), b"2": mail(subject="Automatic reply: away"), b"3": mail(frm="Bob <bob@x.com>", subject="Help")})
        desk = FakeDesk()
        stats, sent = self.go(imap, desk)
        self.assertEqual(stats["logged"], 2)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["acked"], 2)
        self.assertEqual(stats["errors"], 0)
        self.assertIn("P2UNLogged", imap.flags[b"1"])
        self.assertIn("P2UNSkipped", imap.flags[b"2"])
        email_in = [p for a, p in desk.calls if a == "email_in"][0]
        self.assertEqual(len(email_in["images"]), 1)
        # 2 acks + 1 outbox send by SMTP; the draft went to Drafts, copies of sends to Sent.
        self.assertEqual(len(sent), 3)
        folders = [f for f, _, _ in imap.appended]
        self.assertEqual(folders.count('"INBOX.Drafts"'), 1)
        self.assertEqual(folders.count('"INBOX.Sent"'), 3)
        draft_flags = [fl for f, fl, _ in imap.appended if f == '"INBOX.Drafts"'][0]
        self.assertIn("\\Draft", draft_flags)
        done = [p for a, p in desk.calls if a == "outbox_done"]
        self.assertEqual([d["result"] for d in done], ["saved in Drafts", "sent"])
        ping = [p for a, p in desk.calls if a == "ping"][0]
        self.assertTrue(ping["ok"])
        # A second pass finds nothing new.
        desk2 = FakeDesk(); desk2.outbox = []
        stats2, _ = self.go(imap, desk2)
        self.assertEqual(stats2["seen"], 0)

    def test_no_keywords_relies_on_desk(self):
        imap = FakeIMAP({b"1": mail()}, keywords=False)
        stats, _ = self.go(imap, FakeDesk())
        self.assertNotIn("UNKEYWORD", imap.searches[0])
        self.assertEqual(stats["logged"], 1)

    def test_dry_run_changes_nothing(self):
        imap = FakeIMAP({b"1": mail()})
        desk = FakeDesk()
        stats, sent = self.go(imap, desk, dry=True)
        self.assertEqual(sent, [])
        self.assertEqual(desk.calls, [])
        self.assertEqual(imap.flags[b"1"], set())


if __name__ == "__main__":
    unittest.main()
