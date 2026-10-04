# `__main__` guard around CLI dispatch

`bot/anibot.py` — bottom-of-file CLI dispatch block.

Guarded by `if __name__ == "__main__":` so that `import anibot` (e.g. from the test suite)
does NOT launch the bot, while running the module as a script — `python anibot.py [args]`,
the Docker ENTRYPOINT/CMD — still dispatches identically. The if-blocks inside don't create a
new scope, so the module-level globals `botfile`/`botfolder` are reassigned exactly as before.

## Do not

Don't remove this guard or move the dispatch logic to module scope — `import anibot` (the
test suite does this to reach the pure-logic functions) would otherwise launch the bot as a
side effect of the import.
