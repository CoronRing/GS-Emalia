# The mail toolkit

`emalia.mail` is a typed IMAP/SMTP layer that knows nothing about agents, LLMs,
or `railtracks`. Install `emalia` and import it on its own if all you want is
convenient mail access from Python.

Everything below runs without an API key.

## `MailClient`

The facade. One object for reading and sending, with connections opened lazily
and reused across a batch.

```python
from emalia.mail import MailClient, MailAccount

# From EMALIA_* environment variables (a .env file is read on import)
with MailClient.from_env() as mail:
    ...

# Or explicitly
account = MailAccount.for_provider("fastmail", "me@fastmail.com", "app-password")
with MailClient(account, footer="-- \nsent by a script") as mail:
    ...
```

### Reading

```python
mail.list_folders()                       # ['INBOX', 'Sent', 'Archive', ...]
mail.select("Archive")                    # switch the mailbox reads act on

mail.inbox(limit=20)                      # list[EmailMessage], newest first
mail.unread(limit=10)                     # only unread
mail.summaries(limit=50)                  # list[EmailSummary], much cheaper to render

mail.fetch("1234")                        # one message by UID
mail.fetch("1234", mark_read=True)        # and flag it read
mail.fetch("1234", strip_quotes=False)    # keep the quoted thread history

mail.find_by_message_id("<abc@example.com>")
```

`fetch` does **not** mark mail read by default. A listener that flags mail read
before it has successfully answered loses that mail on a crash.

### Searching

`SearchCriteria` builds an IMAP `SEARCH` expression without you writing the
grammar. Values are quoted, so a subject containing a quote cannot break out of
its term.

```python
from emalia.mail import SearchCriteria

uids = mail.search(
    SearchCriteria()
    .unseen()
    .from_("alice@example.com")
    .subject("invoice")
    .since("01-Jun-2026"),
    limit=20,
)
```

| Method | Matches |
|---|---|
| `.unseen()` / `.seen()` | Read state |
| `.from_(v)` / `.to(v)` | Address headers |
| `.subject(v)` | Subject text |
| `.body(v)` / `.text(v)` | Body, or headers and body |
| `.since(d)` / `.before(d)` | Dates, formatted `DD-Mon-YYYY` |
| `.header(name, v)` | Any header |

A raw string works too: `mail.search("UNSEEN FROM alice@example.com")`.

Results come back newest first, which is the opposite of what servers return
and almost always what you want.

### Sending

```python
mail.send(
    to="alice@example.com",
    subject="the report",
    body="Attached.",
    cc=["bob@example.com"],
    bcc=["archive@example.com"],       # stripped from the headers before transmission
    attachments=["~/reports/q3.xlsx", "~/reports/charts/"],  # a directory is zipped
)

mail.reply(message, "Got it, thanks.")           # threads correctly
mail.reply(message, "...", reply_all=True)
mail.reply(message, "...", to=["vetted@example.com"])   # override recipients

mail.forward(message, to="bob@example.com", note="fyi")
```

Replies carry `In-Reply-To` and `References`, so clients group them under the
original, and `Auto-Submitted: auto-replied`, so well-behaved autoresponders
skip them.

### Flags and moving

```python
mail.mark_read(["1234", "1235"])
mail.mark_unread("1234")
mail.flag("1234")                        # sets \Flagged, the star in most clients
mail.move(["1234"], "Archive")
```

### Attachments

```python
paths = mail.save_attachments(message, "~/downloads")
```

Filenames are sanitised of path separators first, so a sender cannot escape the
destination directory by naming a part `../../autorun.inf`. Inline parts
(usually embedded images) are skipped unless you pass `include_inline=True`.

### Connectivity

```python
mail.check()   # {'imap': 'ok', 'smtp': 'Login failed for ...'}
```

Never raises, so you can report both results rather than only the first
failure. This is what `emalia check` calls.

---

## `EmailMessage`

A plain dataclass. No `email.message.Message` leaks through.

```python
message.uid              # IMAP UID, or None for a locally built message
message.message_id       # the Message-ID header, the de-duplication key
message.subject          # decoded, never None
message.sender           # EmailAddress | None
message.to, message.cc, message.bcc, message.reply_to
message.date             # timezone-aware datetime | None
message.text             # the text/plain body
message.html             # the text/html body, if any
message.attachments      # list[Attachment]
message.flags            # IMAP flags as reported at fetch
message.in_reply_to, message.references
message.headers          # every header, lowercased keys

message.body             # text if present, else the HTML rendered down to text
message.reply_target     # Reply-To if set, else the author
message.recipients()     # To + Cc + Bcc
message.summary()        # EmailSummary
```

`body` is the one to reach for. It prefers `text/plain`, falls back to a text
rendering of the HTML part so HTML-only senders are not seen as empty, and by
default has the quoted thread history already removed.

## `EmailAddress`

```python
address = EmailAddress.parse("Alice Smith <Alice@Example.COM>")
address.address    # 'alice@example.com', always lowercase
address.name       # 'Alice Smith'
address.domain     # 'example.com'
str(address)       # 'Alice Smith <alice@example.com>'
```

`parse_address_list` splits a header, correctly ignoring commas inside quoted
display names.

## `Attachment`

```python
attachment.filename        # as declared by the sender
attachment.content_type
attachment.data            # decoded bytes
attachment.size
attachment.inline          # True for embedded images and similar
attachment.safe_filename() # a basename with no separators, safe to join
```

---

## Parsing helpers

Useful on their own.

```python
from emalia.mail import parse_message, strip_quoted_reply, html_to_text, decode_header_value

message = parse_message(raw_bytes, uid="12")
```

`parse_message` never raises on malformed input. A message with a broken header,
an unknown charset, or invalid HTML comes back with those pieces empty rather
than taking down a poll loop.

### `strip_quoted_reply`

Removes quoted history from a reply body: `On <date>, <person> wrote:`
attribution blocks and everything after them, runs of `>` lines,
`-----Original Message-----` separators, and the `-- ` signature delimiter.
French, German and Spanish attribution lines are recognised alongside English.

If stripping would leave nothing, the original text is returned instead — a
noisy body beats an empty one.

### `html_to_text`

Renders an HTML body to readable text, dropping `<script>`, `<style>`,
`<blockquote>`, and the quoted-reply containers Gmail, Thunderbird, Yahoo and
Apple Mail use.

Not a full renderer. The goal is something a person or a model can read, not a
reproduction of the layout.

### `decode_header_value`

Decodes RFC 2047 encoded words: `=?utf-8?B?SGVsbG8=?=` becomes `Hello`.

---

## Lower layers

`MailClient` is a facade over two objects you can use directly when you want
control over connection lifetime.

```python
from emalia.mail import ImapSession, SmtpSender, SearchCriteria

with ImapSession(account, folder="Archive") as imap:
    for uid in imap.search(SearchCriteria().unseen()):
        message = imap.fetch(uid)
    imap.store_flags(["1", "2"], "\\Flagged", action="add")

with SmtpSender(account) as smtp:
    smtp.send(mime_message)
```

`ImapSession` reconnects once on `imaplib.IMAP4.abort`, which is what servers
raise when they drop an idle client, and works in UIDs rather than sequence
numbers, which shift whenever anything else touches the mailbox.

## Composing without sending

```python
from emalia.mail import build_message, build_reply, build_forward, load_attachment

mime = build_message(
    sender=EmailAddress(address="me@example.com", name="Me"),
    to=["alice@example.com"],
    subject="hi",
    body="text",
    html_body="<p>text</p>",
    attachments=["~/file.pdf"],
    footer="-- \nsent by a script",
)
```

Each returns a standard `email.message.EmailMessage`, so it drops into any
other sending path you already have.
