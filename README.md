# p2un-support

The email side of the P2UN Support Desk. Every 2 hours a GitHub Actions job:

1. Reads new mail in **support@p2unlimited.com** over IMAP (Fasthosts, livemail.co.uk). It never marks mail as read.
2. Skips automatic replies, bounces, mailing lists, no-reply senders and its own mail, so it can never loop.
3. Logs each real email as a ticket in the Support Desk Sheet (Apps Script web app), with up to 3 image attachments.
   A reply that carries a ticket number (for example `[GH-0007]`) is added to that ticket instead.
4. Sends the acknowledgement from support@ (at most one per sender every 6 hours, marked `Auto-Submitted`) and keeps a copy in Sent.
5. Works the Desk's outbox: approved replies go to **Drafts** for you to send (or are sent, if the Desk's Reply mode is Send).
6. Pings the Desk's Health tab, so a stopped job shows up in your daily digest.

Triage, fixes and approvals all live in the Support Desk Sheet. This repo only moves mail.

## Settings

Repository **secrets** (Settings, Secrets and variables, Actions):

| Secret | What |
|---|---|
| `MAIL_USER` | `support@p2unlimited.com` |
| `MAIL_PASSWORD` | the mailbox password |
| `SUPPORT_ENDPOINT` | the Support Desk web app link, ending `/exec` |
| `SUPPORT_TOKEN` | the Desk's `SUPPORT_TOKEN` (run `sdShowTokens` in the Desk's Apps Script) |

Optional repository **variables**: `IMAP_HOST` (default `mail.livemail.co.uk`, the Fasthosts IMAP server), `IMAP_PORT` (993), `SMTP_HOST` (`smtp.livemail.co.uk`), `SMTP_PORT` (465; 587 uses STARTTLS), `DRAFTS_FOLDER`, `SENT_FOLDER` (found automatically), `SUPPORT_ADDRESS`.

## Running it by hand

Actions, Support mailbox, Run workflow, then pick a mode:

- `check`: logs in to IMAP, SMTP and the Desk, changes nothing. Do this first.
- `dry`: reads the mailbox and says what it would do, changes nothing.
- `run`: a normal pass.

Locally: set the same environment variables and run `python3 support_mail.py --check`. Tests: `python3 -m unittest discover -s tests -v` (offline, no mailbox needed).

## Minutes

Private repos on the Free plan get 2,000 Actions minutes a month. Every 2 hours uses about 370. To change it, edit the cron line in `.github/workflows/support-mail.yml` (for example `7 */4 * * *` for every 4 hours). Run workflow still works any time for an immediate pass.
