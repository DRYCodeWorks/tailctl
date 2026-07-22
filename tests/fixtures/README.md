# Test fixtures

These are **sanitized minimal JSON samples** that mirror the shape of the
`tailscale switch --list --json` and `tailscale status --json` outputs. They
exist so that unit tests in `tests/test_tailscale.py` (and friends) can parse
deterministic input without shelling out to a real `tailscale` binary.

- Account IDs, tailnet names, hostnames, IPs, and node keys are intentionally
  fake (`100.64.0.x`, `fixture.ts.net`, etc.).
- Tailnet names use the `.example` reserved TLD.
- The structure (top-level keys, nesting, `ExitNodeStatus` shape) was captured
  from a real Tailscale 1.98 install and trimmed.

To recapture from your own machine (writes to `$TAILCTL_HOME/fixtures/`, NOT
to this repo):

```
bash scripts/capture-fixtures.sh
```

If Tailscale's JSON shape ever drifts in a way the tests don't catch,
recapture, sanitize, and update the fixtures here.
