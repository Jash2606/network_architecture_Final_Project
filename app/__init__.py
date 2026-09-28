"""Network Architecture course project: BHTTP/1, HTTP in binary.

Two tracks, one protocol (spec/BHTTP-1.md):
    bserve  -- the server: maps a path to a file under a root, keeps the connection open
    bcurl   -- the client: builds request frames, body to stdout, never opens a second connection

Layout (Model / View / Controller on top of a hand-written network layer):

    models/       what things *are*: frames, requests, responses, files
    views/        what things *look like*: frames on the wire, hexdumps in the terminal
    controllers/  what *happens*: bserve answers a request, bcurl runs a fetch
    network/      sockets and framing -- the framework we were told not to import
"""
