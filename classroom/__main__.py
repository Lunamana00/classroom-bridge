import argparse

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="수업 사이 — 로컬 교육용 MCP 프로토타입")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run("classroom.app:create_app", factory=True, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
