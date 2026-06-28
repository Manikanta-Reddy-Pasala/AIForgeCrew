# codemem test suite — gate index

Each `L<N>_<name>/` directory is a layer gate. Every gate has a
`README.md` beside it that documents the contract (purpose, fixture,
command, pass criteria, expected output, failure remediation).

| Layer | Dir | Plan |
|---|---|---|
| L1 | L1_repo_node/ | plan 1 (this) |
| L2 | L2_service_extract/ | plan 2 |
| L3 | L3_file_summary/ | plan 4 |
| L4 | L4_symbols/ | plan 3 |
| L5 | L5_chunks_vectors/ | plan 5 |
| L6 | L6_translator/ | plan 7 |
| L7 | L7_bundle/ | plan 7 |
| L8 | L8_e2e/ | plan 8 |

Run a single layer:

    make test-codemem-L1

Run all:

    make test-codemem-all
