# Contributing / Como contribuir

Issues and pull requests are welcome, in English or Portuguese.
Issues e pull requests são bem-vindos, em português ou inglês.

1. Open an issue describing the bug (with steps to reproduce) or the idea.
2. One topic per pull request.
3. Run the checks before sending:

```
set JARVIS_SKIP_LLM=1
py -3.12 -m unittest tests.test_hooks tests.test_brain tests.test_confirm -v
```

4. Never commit tokens, keys, logs or personal data.
