"""Allow running muxplex-deck as: python -m muxplex_deck

Needed as the `python -m` fallback the service-management code (launchd's
ProgramArguments, systemd's ExecStart) resolves to when neither the
`muxplex-deck` console-script nor the `~/.local/bin/muxplex-deck` symlink
can be found on PATH.
"""

from muxplex_deck.cli import main

main()
