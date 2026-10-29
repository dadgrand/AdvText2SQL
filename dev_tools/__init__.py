"""Offline developer tooling for the AdvText2SQL harness (non-invasive).

Nothing in this package is imported by the shipped benchmark code: it exists so
that the measurement layer can be tested and debugged without a PostgreSQL
instance, without network access and without an LLM key.

Modules
-------
fixed_eval
    Safe (AST-based) SQLite->PostgreSQL rewrite, gold-query damage audit and the
    execution-accuracy report (EX/241, EX/211, per-difficulty Wilson CIs,
    soft-F1, paired McNemar/bootstrap).
stub_llm
    Deterministic, scriptable, OpenAI-compatible fake chat client used as the
    offline substrate for tool tests.
procedures_dryrun
    Offline proof that the two procedure arms (retrieval few-shot, self-consistency)
    can be enabled with no database, no GPU and no network; writes its transcript
    and the per-question procedure log into ``runs/<out>/``.
"""
