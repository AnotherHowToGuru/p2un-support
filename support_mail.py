"""P2UN support mailbox job.

Reads new mail in support@p2unlimited.com over IMAP, logs each message as a ticket in the P2UN Support Desk
(an Apps Script web app over a Google Sheet), sends the acknowledgement over SMTP, then works through the
Support Desk outbox: approved replies are saved to Drafts (or sent, if the Desk's Reply mode is Send).

Standard library only. Configured by environment variables (GitHub Actions secrets):
  MAIL_USER, MAIL_PASSWORD           the support@ mailbox login
  IMAP_HOST, IMAP_PORT               default mail.livemail.co.uk, 993 (SSL)
  SMTP_HOST, SMTP_PORT               default smtp.livemail.co.uk, 465 (SSL); port 587 uses STARTTLS
  SUPPORT_ENDPOINT                   the Support Desk web app /exec link
  SUPPORT_TOKEN                      the Desk's SUPPORT_TOKEN
  SUPPORT_ADDRESS                    default support@p2unlimited.com
  SUPPORT_NAME                       default P2UN Support
  DRAFTS_FOLDER, SENT_FOLDER         optional; found automatically when blank
  LOOKBACK_DAYS                      default 3
  DRY_RUN=1                          read and decide, change nothing

Run: python support_mail.py            (one pass)
     python support_mail.py --check    (log in to IMAP, SMTP and the Desk, change nothing)
"""

from __future__ import annotations

import base64
import datetime as dt
import email
import email.policy
import imaplib
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from html.parser import HTMLParser

LOGGED_FLAG = "P2UNLogged"
SKIPPED_FLAG = "P2UNSkipped"
MAX_IMAGES = 3
MAX_IMAGE_BYTES = 4_400_000
MAX_ACKS_PER_RUN = 20

AUTO_SUBJECT = re.compile(
    r"^\s*(auto(matic)?[ -]?(reply|response)|out of (the )?office|undeliverable|undelivered|delivery status notification|"
    r"mail delivery (failed|subsystem)|returned mail|failure notice|away from|on vacation|read:|accepted:|declined:)",
    re.I,
)
NO_REPLY_LOCAL = re.compile(r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer-daemon|postmaster|bounce[s]?|notifications?)([+-].*)?$", re.I)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def log(*parts) -> None:
    print(dt.datetime.utcnow().strftime("%H:%M:%S"), *parts, flush=True)


# ---------------------------------------------------------------- the Support Desk API

class Desk:
    def __init__(self, endpoint: str, token: str):
        self.endpoint = endpoint
        self.token = token

    def call(self, action: str, **payload) -> dict:
        body = json.dumps({"token": self.token, "action": action, **payload}).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
        last = None
        for attempt in range(3):
            try:
                # Apps Script answers a POST with a redirect to the result; urllib follows it with a GET.
                with urllib.request.urlopen(req, timeout=90) as res:
                    out = json.loads(res.read().decode("utf-8"))
                if not out.get("ok") and out.get("error") in ("Not allowed", "Unknown action"):
                    raise RuntimeError(f"Support Desk refused {action}: {out.get('error')}")
                return out
            except RuntimeError:
                raise
            except Exception as e:  # network blips, Apps Script busy
                last = e
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"Support Desk call {action} failed: {last}")


# ---------------------------------------------------------------- reading a message

class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        if tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", "".join(p.parts))).strip()


def parse_message(raw: bytes) -> dict:
    """Pulls out what the Desk needs: sender, subject, ids, plain text and small image attachments."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    name, addr = parseaddr(str(msg.get("Reply-To") or msg.get("From") or ""))
    from_name, from_addr = parseaddr(str(msg.get("From") or ""))
    text = ""
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is not None:
        try:
            content = body.get_content()
        except Exception:
            content = (body.get_payload(decode=True) or b"").decode("utf-8", "replace")
        text = html_to_text(content) if body.get_content_type() == "text/html" else content
    images = []
    for part in msg.walk():
        if part.is_multipart() or part is body:
            continue
        ctype = part.get_content_type()
        if not ctype.startswith("image/"):
            continue
        data = part.get_payload(decode=True) or b""
        if not data or len(data) > MAX_IMAGE_BYTES or len(images) >= MAX_IMAGES:
            continue
        # Skip tiny inline images (logos, tracking pixels).
        if len(data) < 2048 and part.get_content_disposition() != "attachment":
            continue
        images.append({"name": part.get_filename() or "image", "type": ctype, "data": base64.b64encode(data).decode("ascii")})
    return {
        "messageId": str(msg.get("Message-ID") or "").strip(),
        "from": (addr or from_addr).strip().lower(),
        "fromHeader": from_addr.strip().lower(),
        "fromName": (name or from_name or "").strip(),
        "subject": str(msg.get("Subject") or "").strip(),
        "date": str(msg.get("Date") or ""),
        "inReplyTo": str(msg.get("In-Reply-To") or "").strip(),
        "references": str(msg.get("References") or "").strip(),
        "text": (text or "").strip()[:20000],
        "images": images,
        "headers": {k.lower(): str(v) for k, v in msg.items()},
    }


def skip_reason(m: dict, support_address: str) -> str:
    """Mail the job must never log or answer: our own mail, automatic replies, bounces, lists and bulk mail."""
    h = m["headers"]
    sender = m["from"]
    local = sender.split("@")[0] if "@" in sender else sender
    if not sender or "@" not in sender:
        return "no sender"
    if sender == support_address.lower() or m["fromHeader"] == support_address.lower():
        return "from ourselves"
    if NO_REPLY_LOCAL.match(local):
        return "no-reply sender"
    auto = h.get("auto-submitted", "").strip().lower()
    if auto and auto != "no":
        return "auto-submitted"
    if h.get("precedence", "").strip().lower() in ("bulk", "junk", "list", "auto_reply"):
        return "bulk precedence"
    if any(k in h for k in ("x-autoreply", "x-autorespond", "x-auto-response-suppress-loop")) or h.get("x-autogenerated"):
        return "autoresponder header"
    if "list-id" in h or "list-unsubscribe" in h:
        return "mailing list"
    if h.get("content-type", "").lower().startswith("multipart/report"):
        return "delivery report"
    if AUTO_SUBJECT.match(m["subject"]):
        return "automatic subject"
    return ""


# ---------------------------------------------------------------- the mailbox

class Mailbox:
    def __init__(self, host: str, port: int, user: str, password: str):
        self.imap = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context())
        self.imap.login(user, password)
        self.keywords_ok = False
        self.drafts = env("DRAFTS_FOLDER")
        self.sent = env("SENT_FOLDER")

    def select_inbox(self) -> None:
        typ, _ = self.imap.select("INBOX")
        if typ != "OK":
            raise RuntimeError("Cannot open INBOX")
        perm = " ".join(str(x) for x in (self.imap.untagged_responses.get("PERMANENTFLAGS") or []))
        self.keywords_ok = "\\*" in perm or LOGGED_FLAG in perm

    def find_folders(self) -> None:
        typ, data = self.imap.list()
        names = []
        for line in data or []:
            s = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
            mm = re.match(r'\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.+)$', s)
            if not mm:
                continue
            flags, name = mm.group("flags"), mm.group("name").strip()
            names.append((flags, name))
            if not self.drafts and "\\Drafts" in flags:
                self.drafts = name
            if not self.sent and "\\Sent" in flags:
                self.sent = name
        plain = {n.strip('"').lower(): n for _, n in names}
        for want, attr in (("drafts", "drafts"), ("sent", "sent")):
            if getattr(self, attr):
                continue
            for cand in ([want, "inbox." + want, "inbox/" + want] + (["sent items", "sent messages", "inbox.sent items"] if want == "sent" else [])):
                if cand in plain:
                    setattr(self, attr, plain[cand])
                    break

    def recent_uids(self, days: int) -> list[bytes]:
        since = (dt.date.today() - dt.timedelta(days=days)).strftime("%d-%b-%Y")
        crit = f'(SINCE {since} UNKEYWORD {LOGGED_FLAG} UNKEYWORD {SKIPPED_FLAG})' if self.keywords_ok else f"(SINCE {since})"
        typ, data = self.imap.uid("SEARCH", None, crit)
        if typ != "OK":
            raise RuntimeError("IMAP search failed")
        return (data[0] or b"").split()

    def fetch(self, uid: bytes) -> tuple[bytes, str]:
        typ, data = self.imap.uid("FETCH", uid, "(FLAGS BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return b"", ""
        return data[0][1], data[0][0].decode("utf-8", "replace")

    def mark(self, uid: bytes, flag: str) -> None:
        if self.keywords_ok:
            self.imap.uid("STORE", uid, "+FLAGS", f"({flag})")

    def append(self, folder: str, msg: EmailMessage, flags: str) -> None:
        if not folder:
            raise RuntimeError("No folder found; set DRAFTS_FOLDER or SENT_FOLDER")
        typ, data = self.imap.append(folder, flags, imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        if typ != "OK":
            raise RuntimeError(f"Could not save to {folder}: {data}")

    def close(self) -> None:
        try:
            self.imap.logout()
        except Exception:
            pass


def build_message(sender_name: str, sender: str, to: str, subject: str, body: str, in_reply_to: str = "", auto: bool = False) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{sender_name} <{sender}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    if auto:
        # RFC 3834: other autoresponders must not answer this.
        msg["Auto-Submitted"] = "auto-replied"
        msg["X-Auto-Response-Suppress"] = "All"
    msg.set_content(body)
    return msg


def smtp_send(msg: EmailMessage) -> None:
    host, port = env("SMTP_HOST", "smtp.livemail.co.uk"), int(env("SMTP_PORT", "465"))
    user, password = env("MAIL_USER"), env("MAIL_PASSWORD")
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=60) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls(context=ctx)
            s.login(user, password)
            s.send_message(msg)


# ---------------------------------------------------------------- one run

def run(dry: bool = False) -> dict:
    support = env("SUPPORT_ADDRESS", "support@p2unlimited.com").lower()
    name = env("SUPPORT_NAME", "P2UN Support")
    desk = Desk(env("SUPPORT_ENDPOINT"), env("SUPPORT_TOKEN"))
    box = Mailbox(env("IMAP_HOST", "mail.livemail.co.uk"), int(env("IMAP_PORT", "993")), env("MAIL_USER"), env("MAIL_PASSWORD"))
    stats = {"seen": 0, "logged": 0, "skipped": 0, "acked": 0, "replies": 0, "errors": 0}
    try:
        box.select_inbox()
        box.find_folders()
        log(f"IMAP ok. Keywords {'on' if box.keywords_ok else 'off (the Desk dedupes by Message-ID)'}; Drafts={box.drafts or '?'}; Sent={box.sent or '?'}")
        for uid in box.recent_uids(int(env("LOOKBACK_DAYS", "3"))):
            raw, meta = box.fetch(uid)
            if not raw or LOGGED_FLAG in meta or SKIPPED_FLAG in meta:
                continue
            stats["seen"] += 1
            m = parse_message(raw)
            why = skip_reason(m, support)
            if why:
                stats["skipped"] += 1
                log(f"skip uid {uid.decode()}: {why}")
                if not dry:
                    box.mark(uid, SKIPPED_FLAG)
                continue
            if dry:
                log(f"would log uid {uid.decode()} from {m['from']}: {m['subject'][:60]}")
                continue
            try:
                res = desk.call("email_in", **{k: m[k] for k in ("messageId", "from", "fromName", "subject", "date", "inReplyTo", "references", "text", "images")})
                stats["logged"] += 1
                log(f"uid {uid.decode()} -> {res.get('ticketId')}{' (existing)' if res.get('existing') else ''}")
                if res.get("ack") and stats["acked"] < MAX_ACKS_PER_RUN:
                    ack = build_message(name, support, res["ackTo"], res["ackSubject"], res["ackBody"], res.get("inReplyTo", ""), auto=True)
                    smtp_send(ack)
                    try:
                        box.append(box.sent, ack, "(\\Seen)")
                    except Exception as e:
                        log(f"ack sent, copy to Sent failed: {e}")
                    desk.call("ack_sent", ticketId=res["ticketId"], result="sent")
                    stats["acked"] += 1
                box.mark(uid, LOGGED_FLAG)
            except Exception as e:
                stats["errors"] += 1
                log(f"uid {uid.decode()} failed: {e}")
        # Outbox: approved replies and acknowledgements for form reports.
        items = [] if dry else desk.call("outbox").get("items", [])
        for it in items:
            try:
                msg = build_message(name, support, it["to"], it["subject"], it["body"], it.get("inReplyTo", ""), auto=it.get("kind") == "Ack")
                if it.get("mode") == "send":
                    smtp_send(msg)
                    try:
                        box.append(box.sent, msg, "(\\Seen)")
                    except Exception as e:
                        log(f"sent, copy to Sent failed: {e}")
                    result = "sent"
                else:
                    box.append(box.drafts, msg, "(\\Draft \\Seen)")
                    result = "saved in Drafts"
                desk.call("outbox_done", id=it["id"], ok=True, result=result)
                stats["replies"] += 1
                log(f"{it['id']} {it.get('kind')} for {it.get('ticketId')}: {result}")
            except Exception as e:
                stats["errors"] += 1
                log(f"{it.get('id')} failed: {e}")
                try:
                    desk.call("outbox_done", id=it["id"], ok=False, result=str(e)[:250])
                except Exception:
                    pass
    finally:
        box.close()
    if not dry:
        detail = ", ".join(f"{k} {v}" for k, v in stats.items())
        try:
            desk.call("ping", component="email-job", client="p2un", ok=stats["errors"] == 0, detail=detail)
        except Exception as e:
            log(f"ping failed: {e}")
    log("done:", stats)
    return stats


def check() -> int:
    """Logs in everywhere and changes nothing."""
    ok = True
    try:
        box = Mailbox(env("IMAP_HOST", "mail.livemail.co.uk"), int(env("IMAP_PORT", "993")), env("MAIL_USER"), env("MAIL_PASSWORD"))
        box.select_inbox()
        box.find_folders()
        log(f"IMAP ok. Keywords {'on' if box.keywords_ok else 'off'}; Drafts={box.drafts or 'NOT FOUND'}; Sent={box.sent or 'NOT FOUND'}")
        box.close()
    except Exception as e:
        ok = False
        log(f"IMAP failed: {e}")
    try:
        host, port = env("SMTP_HOST", "smtp.livemail.co.uk"), int(env("SMTP_PORT", "465"))
        ctx = ssl.create_default_context()
        s = smtplib.SMTP_SSL(host, port, context=ctx, timeout=60) if port == 465 else smtplib.SMTP(host, port, timeout=60)
        if port != 465:
            s.starttls(context=ctx)
        s.login(env("MAIL_USER"), env("MAIL_PASSWORD"))
        s.quit()
        log("SMTP ok.")
    except Exception as e:
        ok = False
        log(f"SMTP failed: {e}")
    try:
        res = Desk(env("SUPPORT_ENDPOINT"), env("SUPPORT_TOKEN")).call("outbox")
        log(f"Support Desk ok. Outbox items waiting: {len(res.get('items', []))}")
    except Exception as e:
        ok = False
        log(f"Support Desk failed: {e}")
    return 0 if ok else 1


if __name__ == "__main__":
    missing = [k for k in ("MAIL_USER", "MAIL_PASSWORD", "SUPPORT_ENDPOINT", "SUPPORT_TOKEN") if not env(k)]
    if missing:
        log("Missing settings: " + ", ".join(missing))
        sys.exit(2)
    if "--check" in sys.argv:
        sys.exit(check())
    stats = run(dry=env("DRY_RUN") == "1")
    sys.exit(1 if stats["errors"] else 0)
