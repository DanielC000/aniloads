# Timer-tolerance assertion for `_move_startup_wait`'s delay path

`tests/test_app.py` — `test_no_trigger_falls_through_after_delay_elapses`.

`threading.Event.wait(timeout)` can return a fraction of a millisecond before the nominal
timeout elapses on Windows (OS timer/scheduler granularity), not because
`_move_startup_wait` itself returns early. Asserting the exact delay value made this test
intermittently fail on Windows hosts/CI runners.

## Do not

Don't assert `elapsed >= delay` (or any exact-value comparison) against a
`threading.Event.wait(timeout)`-based delay in this test — always allow a small tolerance
(`delay - 0.02` here) for OS timer/scheduler granularity, or the test flakes on Windows.
