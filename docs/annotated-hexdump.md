# Annotated hexdump: one complete BHTTP/1 request and response

Captured from a real run, with bserve serving `./www`:

```text
$ ./bserve ./www 9000
$ ./bcurl -v -H "accept-language: en" localhost:9000/hello.txt
```

`./bcurl --annotate ...` prints the same field-by-field breakdown straight from the tool.
All offsets are from the start of each frame, in hex. Three frames cross the wire:
one from the client, then two from the server.

---

## Frame 1: REQUEST, client to server (78 octets)

```text
00000000  bf 01 01 00 01 00 00 46  01 00 0a 2f 68 65 6c 6c  |.......F.../hell|
00000010  6f 2e 74 78 74 01 00 0e  6c 6f 63 61 6c 68 6f 73  |o.txt...localhos|
00000020  74 3a 39 30 30 30 02 00  09 62 63 75 72 6c 2f 31  |t:9000...bcurl/1|
00000030  2e 30 03 00 03 2a 2f 2a  00 00 0f 61 63 63 65 70  |.0...*/*...accep|
00000040  74 2d 6c 61 6e 67 75 61  67 65 00 02 65 6e        |t-language..en|
```

| Offset | Octets | Field | Meaning |
| --- | --- | --- | --- |
| `00` | `bf` | sync | Every frame starts with 0xBF. |
| `01` | `01` | type | REQUEST. |
| `02` | `01` | flags | END_STREAM: this one frame is the whole request. |
| `03` | `00 01` | stream id | Request number 1 on this connection. |
| `05` | `00 00 46` | length | 70 payload octets follow, so this frame ends at offset `08 + 46 = 4e`. |
| `08` | `01` | method | GET. |
| `09` | `00 0a` | path length | 10 octets. |
| `0b` | `2f 68 65 6c 6c 6f 2e 74 78 74` | path | `/hello.txt` |
| `15` | `01` | header | Static index #1 = `host`: the whole name costs one octet. |
| `16` | `00 0e` | value length | 14 |
| `18` | `6c 6f 63 61 6c 68 6f 73 74 3a 39 30 30 30` | value | `localhost:9000` |
| `26` | `02` | header | #2 = `user-agent` |
| `27` | `00 09` | value length | 9 |
| `29` | `62 63 75 72 6c 2f 31 2e 30` | value | `bcurl/1.0` |
| `32` | `03` | header | #3 = `accept` |
| `33` | `00 03` | value length | 3 |
| `35` | `2a 2f 2a` | value | `*/*` |
| `38` | `00` | header | Index 0: this name is not in the table, so a literal follows. |
| `39` | `00 0f` | name length | 15 |
| `3b` | `61 63 63 65 70 74 2d 6c 61 6e 67 75 61 67 65` | name | `accept-language` |
| `4a` | `00 02` | value length | 2 |
| `4c` | `65 6e` | value | `en`. The last octet is `4d`, and the payload ends exactly where its length said. |

The header block has no count and no terminator: it simply fills the rest of the payload.
The same request as HTTP/1.1 text is 106 octets. Here it is 78, and 8 of those are the
frame header.

---

## Frame 2: RESPONSE, server to client (144 octets)

```text
00000000  bf 02 00 00 01 00 00 88  00 c8 04 00 0a 62 73 65  |.............bse|
00000010  72 76 65 2f 31 2e 30 05  00 1d 46 72 69 2c 20 32  |rve/1.0...Fri, 2|
00000020  35 20 53 65 70 20 32 30  32 36 20 31 39 3a 34 36  |5 Sep 2026 19:46|
00000030  3a 35 30 20 47 4d 54 06  00 19 74 65 78 74 2f 70  |:50 GMT...text/p|
00000040  6c 61 69 6e 3b 20 63 68  61 72 73 65 74 3d 75 74  |lain; charset=ut|
00000050  66 2d 38 07 00 02 33 30  08 00 1d 46 72 69 2c 20  |f-8...30...Fri, |
00000060  32 35 20 53 65 70 20 32  30 32 36 20 31 39 3a 34  |25 Sep 2026 19:4|
00000070  33 3a 34 37 20 47 4d 54  09 00 15 22 31 65 2d 31  |3:47 GMT..."1e-1|
00000080  38 64 38 61 37 64 65 64  39 33 30 39 32 33 63 22  |8d8a7ded930923c"|
```

| Offset | Octets | Field | Meaning |
| --- | --- | --- | --- |
| `00` | `bf` | sync | |
| `01` | `02` | type | RESPONSE |
| `02` | `00` | flags | No END_STREAM, so DATA frames follow. |
| `03` | `00 01` | stream id | The answer to request 1. |
| `05` | `00 00 88` | length | 136 payload octets, so the frame ends at `08 + 88 = 90`. |
| `08` | `00 c8` | status | 200 (OK) |
| `0a` | `04` | header | #4 = `server` |
| `0b` | `00 0a` | value length | 10 |
| `0d` | `62 73 65 72 76 65 2f 31 2e 30` | value | `bserve/1.0` |
| `17` | `05` | header | #5 = `date` |
| `18` | `00 1d` | value length | 29 |
| `1a` | `46 72 69 ... 47 4d 54` | value | `Fri, 25 Sep 2026 19:46:50 GMT` |
| `37` | `06` | header | #6 = `content-type` |
| `38` | `00 19` | value length | 25 |
| `3a` | `74 65 78 ... 2d 38` | value | `text/plain; charset=utf-8` |
| `53` | `07` | header | #7 = `content-length` |
| `54` | `00 02` | value length | 2 |
| `56` | `33 30` | value | `30`: the client will expect 30 body octets in total. |
| `58` | `08` | header | #8 = `last-modified` |
| `59` | `00 1d` | value length | 29 |
| `5b` | `46 72 69 ... 47 4d 54` | value | `Fri, 25 Sep 2026 19:43:47 GMT` |
| `78` | `09` | header | #9 = `etag` |
| `79` | `00 15` | value length | 21 |
| `7b` | `22 31 65 ... 63 22` | value | `"1e-18d8a7ded930923c"`, the size in hex plus the mtime in nanoseconds. It ends at `8f`. |

All six names came from the static table. Every name cost one octet, and the whole
header block is 134 octets.

---

## Frame 3: DATA, server to client (38 octets)

```text
00000000  bf 03 01 00 01 00 00 1e  50 6c 61 69 6e 20 74 65  |........Plain te|
00000010  78 74 2c 20 73 65 72 76  65 64 20 62 79 20 62 73  |xt, served by bs|
00000020  65 72 76 65 2e 0a                                 |erve..|
```

| Offset | Octets | Field | Meaning |
| --- | --- | --- | --- |
| `00` | `bf` | sync | |
| `01` | `03` | type | DATA |
| `02` | `01` | flags | END_STREAM: this is the last frame of response 1. |
| `03` | `00 01` | stream id | 1 |
| `05` | `00 00 1e` | length | 30 octets, which matches `content-length: 30`. |
| `08` | `50 6c 61 ... 2e 0a` | body | `Plain text, served by bserve.\n`, written to stdout by bcurl. |

---

## Where each thing ends, and what happens next

* **The request** ends when frame 1 ends, at 8 + 70 octets. END_STREAM on it says no
  body follows.
* **The response** ends at the first frame for stream 1 with END_STREAM: frame 3. The
  connection does not have to close to say so. The next octet on the wire would be the
  sync byte of whatever comes next.
* **The connection** stays open. bcurl could send REQUEST for stream 2 on it right away
  (`bcurl a b c` does). The server closes it with GOAWAY 408 only after 60 s of silence.
* **An unknown frame** (try `bcurl --grease`) would look like `bf f7 3c 00 00 00 00 11`
  plus 17 octets. The receiver cannot understand it, but it can read `00 00 11` = 17,
  discard that many octets, and land exactly on the next `bf`.
