# Contributing

Bug reports and pull requests are welcome.

## Set up

```bash
git clone https://github.com/AbuCTF/Hutch.git && cd Hutch
uv venv && uv pip install -e ".[all,dev]"     # or ./setup.sh, which uses pip
.venv/bin/hutch doctor
.venv/bin/python -m pytest -q
```

Tests launch a real browser (Google Chrome by default) against local servers, so they need Chrome,
and the virtual display tests need Xvfb. Nothing in the suite contacts the internet.

## Guidelines

- Keep changes small and focused, with a test for behavior you add or fix.
- Match the existing style: no comments that restate code, plain names, standard library first.
- Do not add dependencies without a reason; runtime dependencies stay Apache-2.0 or MIT.
- Never commit credentials, cookies, real account names or details of sites you tested.
  Use `example.com` and local test servers in tests and examples.

## Commits

The subject is the version only (`v0.31.1`) and the body is a list of terse lowercase
`area: change` bullets:

```
v0.31.1

- doctor: a missing bundled chromium is not a failure when chrome is installed
- readme: install, hello world and clear_challenge
```

Versions follow semver: patch for increments, minor for new capability.

## Reporting problems

Include `hutch --version`, `hutch doctor` output, your OS, and the smallest script that shows the problem.
Do not post anything that identifies the sites or accounts involved. For a security issue, email the
address on the maintainer's GitHub profile instead of opening a public issue.
