"""ログの出力先を決める。**親プロセスと子プロセスの両方から呼ぶ。**

`cli.py` に置いたままでは、`scan` のワーカープロセスから呼べない。
ワーカーは `spawn` で起こす（`scanner.WORKER_START_METHOD`）ので、
**子は白紙のインタプリタとして始まり、親が付けたログのハンドラも
引き継がない。** 子で `cli` を import すると、循環参照と、CLI 一式
(`converter` / `selection` など) の読み込みコストを毎ワーカー抱える。

ここを独立させておくことで、子は必要なものだけを読んでログを張り直せる。
"""

import logging
from pathlib import Path
from typing import Optional, Union

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"


def setup_logging(
    log_file: Optional[Union[str, Path]] = None, log_level: str = "WARNING"
) -> logging.Logger:
    """`photoarchive` ロガーの出力先を決める。

    **同じプロセスで2度呼んでも重複しない**ようにハンドラを入れ替える。
    ワーカーの入口から呼ぶことを前提にしているため、呼ばれる回数を
    呼び出し側に気にさせない。
    """
    logger = logging.getLogger("photoarchive")
    level = getattr(logging, log_level.upper(), None)
    if not isinstance(level, int):
        raise ValueError(f"Invalid log level: {log_level}")
    logger.setLevel(level)
    logger.handlers.clear()

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path)
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(file_handler)

    return logger
