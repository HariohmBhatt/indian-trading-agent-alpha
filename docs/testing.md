# Testing

## Python test suite

The repository's Python tests use the standard-library `unittest` runner. They
use temporary SQLite databases and mock external service calls, so this gate
does not require API keys, live market data, containers, or a deployment
environment.

After installing the project dependencies, run:

```bash
TRADINGAGENTS_HOME="$(mktemp -d)" \
  python -m unittest discover -s tests -p 'test_*.py' -v
```

This is the same command used by the GitHub-hosted Python test workflow.
