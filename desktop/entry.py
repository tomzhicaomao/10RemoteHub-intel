import sys
from pathlib import Path

# The packaged console executable 10remotehub-cli shares this entry point.
if Path(sys.executable).stem.lower() == "10remotehub-cli":
    from remotehub.cli import main
else:
    from remotehub.app import main
raise SystemExit(main())
