# BHTTP/1: HTTP, in binary

Version 1 · Network Architecture course project · Author: **23bcs10163**

BHTTP/1 carries HTTP-style requests and responses over **one TCP connection** as
length-prefixed binary frames. A client may send many requests on that connection,
including before earlier answers arrive (pipelining). The server answers them **in
the order they arrived**, and the connection stays open until one side closes it.
All integers are unsigned and big-endian. MUST, SHOULD and MAY are used as in RFC 2119.

## 1. The frame header: 8 octets, the same for every frame, forever

```text
 0        1        2        3        4        5        6        7
+--------+--------+--------+--------+--------+--------+--------+--------+
|  Sync  |  Type  | Flags  |    Stream ID    |          Length          |
|  0xBF  |   8    |   8    |       16        |            24            |
+--------+--------+--------+--------+--------+--------+--------+--------+
                              then exactly Length octets of payload
```

| Field | Bits | Meaning |
| --- | --- | --- |
| Sync | 8 | Always `0xBF` ("Binary Frame"). Anything else means the stream is out of step. |
| Type | 8 | What the payload is (section 2). |
| Flags | 8 | Type-specific bits. `0x01` = END_STREAM, set on the last frame of a request or response. |
| Stream ID | 16 | Which request this frame belongs to. `0` means the connection itself. |
| Length | 24 | Payload octets that follow, 0 to 16,777,215. |

A receiver reads exactly 8 octets, then exactly Length octets. **That is where a frame
ends.** Nothing else in the protocol marks a boundary, and no receiver may read past it.

**A receiver meeting a frame type it does not know MUST skip it cleanly**: it reads
and discards Length payload octets, then carries on with the next frame. Undefined flag
bits MUST be sent as 0 and MUST be ignored on receipt.

**Why these widths.** HTTP/2 chose Length 24 / Type 8 / Flags 8 / Stream 31 (+1 reserved
bit), 9 octets in all. It *multiplexes* many streams over one connection, so each frame
must be small enough not to block the others (16 KiB by default). 24 bits leaves room to
negotiate bigger frames and saves an octet over 32. Its stream IDs are never reused and
are split odd/even between client and server, so a long-lived connection needs a huge
ID space. The reserved bit is a SPDY leftover that also keeps IDs positive in languages
without unsigned 32-bit integers. BHTTP/1 has different needs, so it makes different
choices:

* **Sync, 8 bits: the field HTTP/2 does not have.** HTTP/2 has a connection preface,
  ALPN and years of interop testing. We have two strangers and a two-page document. A
  constant first octet makes a framing bug fail loudly *at the frame where it happened*.
  Without it, the bug shows up as a hang or a garbage length 16 MB later. It also rejects
  a plain-text HTTP client (`G` = 0x47) or a TLS client (0x16) on its first byte. Cost:
  one octet per frame.
* **Type 8, Flags 8**, the same as HTTP/2. They are the smallest fields a byte-oriented
  parser can handle without bit twiddling. 256 types is room for many versions.
* **Stream ID 16, not 31.** v1 answers in order and never pushes. The ID only has to be
  unique among requests *in flight*, so the client can check an answer belongs to its
  question, and so that v2 can interleave. 65,535 in flight is far beyond any real
  pipeline, so IDs wrap. This saves two octets per frame.
* **Length 24.** 16 MiB per frame is plenty, and a big file is just several DATA frames.
  16 bits would be enough for v1, but this header is the one thing v2 can never change,
  so we buy headroom. 32 bits would let one frame claim 4 GiB, which a naive receiver
  turns into a 4 GiB allocation.
* **8 octets in all**: one 64-bit word, and exactly half a 16-octet hexdump row.

## 2. Frame types

| Type | Name | Sent by | Payload |
| --- | --- | --- | --- |
| `0x01` | REQUEST | client | `method(8)` `path(str)` `header block` |
| `0x02` | RESPONSE | server | `status(16)` `header block` |
| `0x03` | DATA | server | body octets |
| `0x04` | GOAWAY | either, stream 0 | `last-stream(16)` `code(16)` `reason (UTF-8, rest of payload)` |
| `0xF0`-`0xFF` | *reserved* | anyone | Never assigned, so these are always unknown (section 5). |

**Methods:** `0x01` GET, `0x02` HEAD, `0x03` POST, `0x04` PUT, `0x05` DELETE.
**Status** is an HTTP status code, 100 to 599.

## 3. Strings and header blocks

A **str** is `length(16)` followed by that many octets of UTF-8, with no NUL, CR or LF.
A **path** is a str beginning with `/`. It may carry `%XX` escapes and a `?query`.

A **header block** fills the rest of its payload. It is a sequence of fields, each
`index(8)` then, if index is 0, `name(str)`, and always `value(str)`. This is HPACK's
first two mechanisms and nothing more: no dynamic table and no Huffman coding.

* **Index 1-10**: the ten names our programs actually send cost one octet.

  | # | name | # | name | # | name | # | name | # | name |
  |---|---|---|---|---|---|---|---|---|---|
  | 1 | host | 3 | accept | 5 | date | 7 | content-length | 9 | etag |
  | 2 | user-agent | 4 | server | 6 | content-type | 8 | last-modified | 10 | allow |

* **Index 0**: any other name follows as a literal str, in lower case (for example
  `accept-language`).
* **Index 11-255**: unassigned. A receiver MUST skip the field (read its value, drop it).

## 4. Exchanges

**Request.** A request is one REQUEST frame with END_STREAM set. v1 requests have no
body, and a server MUST skip any DATA frames a client sends. The client MUST send `host`.
It numbers requests 1, 2, 3, ..., wrapping from 65535 back to 1, and MUST NOT reuse an ID
that is still in flight.

**Response.** A response is one RESPONSE frame followed by zero or more DATA frames,
all carrying the request's stream ID. The last frame has END_STREAM; with no body, the
RESPONSE frame itself carries it. If `content-length` is sent, the DATA lengths MUST add
up to it. A HEAD response has the headers GET would have, and no DATA.

**The server** maps the path, without its query, to a file under its root. It
percent-decodes the path and maps a directory to its `index.html`. It sends
`content-type`, `content-length`, `last-modified`, `etag`, `server` and `date`. Errors
carry a short `text/plain` body. **Nothing is an error on the connection unless the
framing is lost:**

| What happened | Server sends | Connection |
| --- | --- | --- |
| File found | RESPONSE 200 + DATA | stays open |
| No such file | 404 | stays open |
| Method other than GET or HEAD | 405 + `allow: GET, HEAD` | stays open |
| Payload malformed (a str runs past the payload, bad UTF-8, path without `/`, stream ID 0), or the path climbs above the root | 400 | stays open, because the frame's Length was intact |
| REQUEST payload over 65,535 octets | skip it, then 400 | stays open |
| Bad sync octet, or EOF inside a frame | GOAWAY code 400 | closed: nobody knows where the next frame starts |
| 60 s with no frame, or 30 s to finish a started frame | GOAWAY code 408 | closed |

**GOAWAY** says "I am closing". *last-stream* is the highest request ID the sender
processed. The sender closes right after sending it. Codes reuse HTTP numbers: 400
broken framing, 408 timeout, 503 too busy. **A client** exits with a non-zero status
on 4xx/5xx, on GOAWAY, or when the connection ends mid-response.

## 5. Leaving room for version 2

The fixed header never changes, so any v1 receiver can find the end of any future frame.
On top of that:

* **Unknown types are skipped**, so v2 can add frame types. **Unknown flags are
  ignored**, so v2 can add flags. **Unknown header indices are skipped**, so v2 can
  grow the table.
* **Types `0xF0`-`0xFF` are never assigned.** Implementations SHOULD send one now and
  then (our `--grease` option). A peer whose skip logic is broken is then caught
  today, not the day v2 ships.
* **No version field is needed.** A v2 client sends a new HELLO frame before its first
  REQUEST. A v1 server skips HELLO, and because answers come in order, the client knows
  the server is v1 when the first frame back is a RESPONSE instead of a reply to HELLO.

## 6. Test vectors

Check an implementation against these bytes. An encoder given these messages MUST
produce exactly these octets, and a decoder MUST read them back as shown. The test
suite checks our own code against them.

**V1: a request.** `GET /index.html` on stream 1, with `host: localhost:9000` (table
entry #1) and `dnt: 1` (a literal name):

```text
bf 01 01 00 01 00 00 28   01   00 0b 2f 69 6e 64 65 78 2e 68 74 6d 6c
01 00 0e 6c 6f 63 61 6c 68 6f 73 74 3a 39 30 30 30   00 00 03 64 6e 74 00 01 31
```

**V2: its answer**, as one RESPONSE frame then one DATA frame. The status is 200, with
`content-type: text/plain`, `content-length: 2`, and the body `hi`:

```text
bf 02 00 00 01 00 00 13   00 c8   06 00 0a 74 65 78 74 2f 70 6c 61 69 6e   07 00 01 32
bf 03 01 00 01 00 00 02   68 69
```

**V3: a frame nobody knows.** Type `0xF7` (reserved) on stream 0, carrying `abc`. A
receiver MUST skip these 11 octets and carry on with whatever comes next:

```text
bf f7 00 00 00 00 00 03   61 62 63
```

Every octet of a longer, real exchange is annotated in `docs/annotated-hexdump.md`.
