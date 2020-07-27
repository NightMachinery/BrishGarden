import uvicorn, sys
# from .garden import app

def main():
    root_path = ''
    if len(sys.argv) >= 2:
        root_path = sys.argv[1]
    uvicorn.run("brishgarden.garden:app", host="127.0.0.1", port=7230, log_level="info", proxy_headers=True, root_path=root_path)
