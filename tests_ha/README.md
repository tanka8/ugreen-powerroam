# Home Assistant integration tests

These run the config flow through Home Assistant itself, using
`pytest-homeassistant-custom-component`.

**They only run on Linux and macOS.** On Windows the harness fails at fixture setup:
Home Assistant's test loop needs a socketpair for the Proactor event loop, and
`pytest-socket` blocks it. That is a limitation of the harness, not of these tests.

```bash
pip install pytest-homeassistant-custom-component
python -m pytest tests_ha/
```

CI runs them on Ubuntu. The tests in `../tests/` need no Home Assistant and run
everywhere, including Windows.
