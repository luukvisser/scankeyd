# Contributing

Reports for printers other than the DCP-L2627DWE are the most useful
contribution: open an issue with the "Printer report" template.

For code changes:

- Keep the daemon on the standard library. OCR, straightening and uploads are
  separate steps on the output folder (see the README), not part of scankeyd.
- Treat everything from the network as untrusted input, and add a test,
  against the fake printer in `tests/fakeprinter.py` where it touches the
  printer.
- Run the tests (see the README) before opening a pull request.
- Describe protocol findings from your own observations; do not add Brother's
  code, binaries or decompiled material.

## AI-assisted contributions

This project itself was written with an AI assistant (Claude, in Claude Code),
with every change reviewed by its maintainer and tested on a real printer; the
commits say so. AI-assisted contributions are welcome on the same terms:

- You understand every line you submit and can explain it in the review.
- You have tested it; for protocol changes, on a real printer (name the model).
- You say so: an `Assisted-by:` line in the commit or the pull request, naming
  the tool and model.
- You have the right to submit it under this project's licence.

Not wanted: large unrequested changes that nobody can explain without the tool
that wrote them, and issues or security reports that no person has checked.
A security report needs a reproduction (see [SECURITY.md](SECURITY.md)).

Scans are often private documents. Do not paste real scans, logs with names
or addresses, or captures from your network into an AI tool or into an issue;
use a test page instead.

Instructions for coding agents are in [AGENTS.md](AGENTS.md).
