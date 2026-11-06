"""启动 wiki_kb_service：python run.py [--host 0.0.0.0] [--port 13105]"""
import argparse

import uvicorn

from app import config

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    args = parser.parse_args()
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=False)
