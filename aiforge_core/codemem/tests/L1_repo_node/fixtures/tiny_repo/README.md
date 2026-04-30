# tiny_repo — codemem L1 fixture

Toy repo used by Stage 1+2 ingest tests. Real enough that
RepoMix produces non-trivial output and the LLM has something
to summarize.

## Build / run

    make build
    make test
    python src/main.py

## Port-forward

    kubectl port-forward svc/tiny-repo 8080:8080 -n default
