import sys

if sys.version_info < (3, 10):   # before importing what needs 3.10, so the line below is all an older Python shows
    sys.exit("kimeru: Python 3.10 以上が必要です（この Python は " + sys.version.split()[0] + "）。"
             "python --version で確かめ、3.10 以上を入れてから実行してください")   # no f-string: Python 3.5 and older read this file too

from .cli import main  # noqa: E402

sys.exit(main())
