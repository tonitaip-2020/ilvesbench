# Ministral 3 8B evaluation summary

| Database | LLM | Normalization | Data migration | Query rewrites |
|---|---|---|---|---:|
| DVD Rental | Ministral 3 8B Instruct | Fail model-only; pass system-assisted | Pass after PostgreSQL prerequisite repair | 100% (10/10) |
| NYC Yellow Taxi 5000 | Ministral 3 8B Instruct | Pass; no decomposition needed | Pass; no-op | 100% (5/5) |
| IMDb 5000 | Ministral 3 8B Instruct | Fail model-only; pass system-assisted | Pass after privilege setup | 100% (10/10) |

The query percentages report empirical PostgreSQL execution and strict equality of output-column names, column order, and ordered result rows on the evaluated datasets. They do not constitute formal equivalence proofs for every possible database state.

“System-assisted” means the completed IlvesBench workflow succeeded through deterministic evidence, PostgreSQL feedback, and human approval even though the raw model normalization response did not satisfy the required structured-output contract.
