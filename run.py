import argparse

import uvicorn


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jalankan Alir di komputer lokal.")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    print(f"\nAlir siap dibuka: http://127.0.0.1:{args.port}\nTekan Ctrl+C untuk berhenti.\n")
    uvicorn.run("app.main:app", host="127.0.0.1", port=args.port)
