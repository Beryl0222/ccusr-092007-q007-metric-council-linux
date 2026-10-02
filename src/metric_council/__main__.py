"""本地启动：``python -m metric_council [port]``。

仅用于对接调试与演示；生产环境请把 :func:`metric_council.api.create_app`
挂到正式 WSGI 容器，并在反向代理层完成身份认证后透传
``X-Actor-Id`` / ``X-Actor-Role``。
"""

from __future__ import annotations

import sys
from wsgiref.simple_server import make_server

from .api import create_app
from .service import CouncilService


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    app = create_app(CouncilService())
    print(f"metric-council 审议后端监听 http://127.0.0.1:{port}")
    with make_server("127.0.0.1", port, app) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
