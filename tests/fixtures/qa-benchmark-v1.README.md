# Frozen synthetic QA matrix

`qa-benchmark-v1.json` is the original eight-case synthetic QA matrix recovered
from the existing private checkout. Its bytes match the existing `PLAN_SHA` in
`scripts/qa_benchmark.py`:

`f01fc2787eceabf198090b1a6e99a04a12d440abfa58b2b449e1be8ca82c5bf8`.

No hash, expected label, model selection or approval field was regenerated to
make tests pass. The adversarial instruction in the last case is untrusted test
input, not an instruction to an operator or model running repository maintenance.
The tests use `httpx.MockTransport`; their receipts cannot grant publication or
stand in for real model-quality acceptance. This file is a test fixture, not an
installed production benchmark policy or an authorization to make model calls.
