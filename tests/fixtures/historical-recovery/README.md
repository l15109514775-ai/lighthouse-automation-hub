# Immutable historical recovery inputs

These two files are test data read only as bytes, never imported as downloaders.
They reproduce the exact source contracts of the old one-off recovery operators;
the current runtime is intentionally different and must remain different.

- `youtube_natural_plan_v3.py.source`: original private commit
  `8a0e9886663d9a9f4dc6a4c9f432f818dfcde256`, SHA-256
  `e6763f9a8ec4a4a2fe95ccd061a36d4701499a9f32cc0797cace4b8ce2467ee9`.
- `ytdlp_whole_v4.py.source`: original private commit
  `1a811c18cbea1bce3b2623500995318bc2917dba`, SHA-256
  `8e3fea2da8cbf13a64533eb62ffd5eb990c3f2f092ef47258edb8f63f6afd148`.

The fixed digests in the operators are unchanged. Tests use real temporary SQLite
and owned policy/root-path seams to check the old transaction/attempt/partial
preservation behavior. Additional negative cases copy today's actual source and
verify rejection without job or partial-file changes. Passing the historical
fixture does not qualify these old operators for today's production runtime.
No old source is installed over a current module and no production recovery is run.
